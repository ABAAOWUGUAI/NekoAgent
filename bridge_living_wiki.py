#!/usr/bin/env python3
"""Auditable lifecycle and rebuildable search projections for Living Wiki."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Mapping


FRESHNESS_STATUSES = {"fresh", "stale", "expired", "unverified"}
_SENSITIVE_REF = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:token|api[_-]?key|password|cookie)=)",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def content_hash(content: object) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def source_hash(item: Mapping[str, object]) -> str:
    payload = {
        key: str(item.get(key) or "")
        for key in (
            "source_type", "source_ref", "source_memory_id", "source_thread_id",
            "source_scope_type", "consent_basis",
        )
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()


def normalize_evidence_refs(value: object) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str) and value.lstrip().startswith("["):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("knowledge_evidence_refs_invalid") from exc
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("knowledge_evidence_refs_invalid")
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise ValueError("knowledge_evidence_refs_invalid")
        item = raw.strip()
        if not item or len(item) > 300 or "\x00" in item or _SENSITIVE_REF.search(item):
            raise ValueError("knowledge_evidence_refs_invalid")
        if item not in result:
            result.append(item)
    return result


def normalize_freshness(value: object, *, default: str = "unverified") -> str:
    selected = str(value or default).strip().lower()
    if selected not in FRESHNESS_STATUSES:
        raise ValueError("knowledge_freshness_invalid")
    return selected


def normalize_timestamp(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"knowledge_{field}_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def effective_freshness(item: Mapping[str, object], *, now: str | None = None) -> str:
    if str(item.get("status") or "") in {"archived", "rejected"}:
        return "expired"
    freshness = normalize_freshness(item.get("freshness_status"), default="unverified")
    fresh_until = str(item.get("fresh_until") or "")
    if freshness == "fresh" and fresh_until:
        try:
            deadline = datetime.fromisoformat(fresh_until.replace("Z", "+00:00"))
            reference = datetime.fromisoformat(str(now or _now()).replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=timezone.utc)
            if deadline.astimezone(timezone.utc) < reference.astimezone(timezone.utc):
                return "stale"
        except ValueError:
            # Stored lifecycle timestamps are validated on write. Historical or
            # manually altered invalid values must never be treated as fresh.
            return "unverified"
    return freshness


def lifecycle_values(
    payload: Mapping[str, object],
    *,
    current: Mapping[str, object] | None = None,
    published: bool = False,
) -> dict:
    current = current or {}
    evidence = normalize_evidence_refs(
        payload.get("evidence_refs", current.get("evidence_refs_json", [])),
    )
    default_freshness = "fresh" if published else str(current.get("freshness_status") or "unverified")
    freshness = normalize_freshness(payload.get("freshness_status"), default=default_freshness)
    verified = normalize_timestamp(
        payload.get("last_verified_at", current.get("last_verified_at") or (_now() if published else "")),
        "last_verified_at",
    )
    fresh_until = normalize_timestamp(
        payload.get("fresh_until", current.get("fresh_until") or ""),
        "fresh_until",
    )
    return {
        "evidence_refs_json": json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
        "freshness_status": freshness,
        "fresh_until": fresh_until,
        "last_verified_at": verified,
    }


def _row_dict(row: sqlite3.Row | tuple, columns: list[str] | None = None) -> dict:
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(zip(columns or [], tuple(row)))


def _snapshot(item: Mapping[str, object]) -> str:
    excluded = {"evidence_refs"}
    return json.dumps(
        {key: item[key] for key in item if key not in excluded},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def record_knowledge_revision(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    operation: str,
    actor: str,
) -> dict:
    row = conn.execute("SELECT * FROM assistant_knowledge_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise ValueError("knowledge_not_found")
    item = _row_dict(row)
    revision_id = f"knowledge-revision-{item_id}-v{int(item['version'])}"
    conn.execute(
        """INSERT INTO assistant_knowledge_revisions(
            id,assistant_id,item_id,version,operation,snapshot_json,content_hash,
            source_hash,evidence_refs_json,freshness_status,created_by,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            revision_id, item["assistant_id"], item_id, item["version"], str(operation)[:40],
            _snapshot(item), item["content_hash"], item["source_hash"],
            item["evidence_refs_json"], item["freshness_status"], str(actor)[:80], _now(),
        ),
    )
    return {
        "id": revision_id,
        "item_id": item_id,
        "version": int(item["version"]),
        "operation": str(operation)[:40],
    }


def search_backend(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT backend FROM assistant_knowledge_search_meta WHERE id=1",
    ).fetchone()
    return str(row[0]) if row else "like_fallback"


def sync_search_item(conn: sqlite3.Connection, item_id: str) -> None:
    row = conn.execute(
        """SELECT id,assistant_id,title,summary,content,tags_json
           FROM assistant_knowledge_items WHERE id=?""",
        (item_id,),
    ).fetchone()
    if not row:
        return
    backend = search_backend(conn)
    table = "assistant_knowledge_search_fts" if backend == "fts5_trigram" else "assistant_knowledge_search_fallback"
    conn.execute(f"DELETE FROM {table} WHERE item_id=?", (item_id,))
    conn.execute(
        f"INSERT INTO {table}(item_id,assistant_id,title,summary,content,tags) VALUES(?,?,?,?,?,?)",
        tuple(row),
    )
    conn.execute(
        "UPDATE assistant_knowledge_search_meta SET updated_at=? WHERE id=1",
        (_now(),),
    )


def rebuild_knowledge_search_index(conn: sqlite3.Connection) -> dict:
    backend = search_backend(conn)
    table = "assistant_knowledge_search_fts" if backend == "fts5_trigram" else "assistant_knowledge_search_fallback"
    conn.execute(f"DELETE FROM {table}")
    rows = conn.execute(
        "SELECT id FROM assistant_knowledge_items ORDER BY id",
    ).fetchall()
    for row in rows:
        sync_search_item(conn, str(row[0]))
    return {"backend": backend, "indexed": len(rows), "rebuilt_at": _now()}


def search_projection(
    conn: sqlite3.Connection,
    assistant_id: str,
    query: str,
    *,
    limit: int = 100,
) -> list[dict]:
    text = " ".join(str(query or "").split())[:500]
    if not text:
        return []
    maximum = max(1, min(int(limit), 200))
    backend = search_backend(conn)
    if backend == "fts5_trigram" and len(text) >= 3:
        expression = '"' + text.replace('"', '""') + '"'
        try:
            rows = conn.execute(
                """SELECT item_id,bm25(assistant_knowledge_search_fts) AS rank
                   FROM assistant_knowledge_search_fts
                   WHERE assistant_knowledge_search_fts MATCH ? AND assistant_id=?
                   ORDER BY rank LIMIT ?""",
                (expression, assistant_id, maximum),
            ).fetchall()
            return [
                {"item_id": str(row[0]), "rank": float(row[1]), "backend": backend}
                for row in rows
            ]
        except sqlite3.OperationalError:
            pass
    pattern = "%" + text.replace("%", "\\%").replace("_", "\\_") + "%"
    table = "assistant_knowledge_search_fallback" if backend == "like_fallback" else "assistant_knowledge_items"
    id_column = "item_id" if table.endswith("fallback") else "id"
    rows = conn.execute(
        f"""SELECT {id_column} FROM {table} WHERE assistant_id=? AND
            (title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR
             content LIKE ? ESCAPE '\\') LIMIT ?""",
        (assistant_id, pattern, pattern, pattern, maximum),
    ).fetchall()
    return [{"item_id": str(row[0]), "rank": 0.0, "backend": "like_fallback"} for row in rows]


def record_retrieval_audit(
    conn: sqlite3.Connection,
    *,
    assistant_id: str,
    query: str,
    channel: str,
    item_id: str,
    signals: Mapping[str, object],
    score: float,
    injected: bool,
    reason: str,
) -> None:
    conn.execute(
        """INSERT INTO assistant_knowledge_retrieval_audit(
            assistant_id,query_hash,channel,item_id,signals_json,score,injected,reason,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            assistant_id, hashlib.sha256(str(query or "").encode("utf-8")).hexdigest(),
            str(channel or "")[:40], str(item_id or "")[:120],
            json.dumps(dict(signals), sort_keys=True, separators=(",", ":")),
            float(score), 1 if injected else 0, str(reason or "")[:80], _now(),
        ),
    )


def list_knowledge_revisions(
    conn: sqlite3.Connection,
    *,
    assistant_id: str = "",
    item_id: str,
    limit: int = 50,
) -> list[dict]:
    if not item_id:
        raise ValueError("knowledge_item_id_required")
    scoped_assistant = assistant_id or str(conn.execute(
        "SELECT id FROM assistant_instances WHERE status='active' ORDER BY created_at LIMIT 1",
    ).fetchone()[0])
    rows = conn.execute(
        """SELECT id,item_id,version,operation,content_hash,source_hash,
                  evidence_refs_json,freshness_status,created_by,created_at
           FROM assistant_knowledge_revisions WHERE assistant_id=? AND item_id=?
           ORDER BY version DESC LIMIT ?""",
        (scoped_assistant, item_id, max(1, min(int(limit), 100))),
    ).fetchall()
    return [
        _row_dict(row) | {"evidence_refs": json.loads(str(row[6] or "[]"))}
        for row in rows
    ]


def list_retrieval_audit(
    conn: sqlite3.Connection,
    *,
    assistant_id: str = "",
    limit: int = 50,
) -> list[dict]:
    scoped_assistant = assistant_id or str(conn.execute(
        "SELECT id FROM assistant_instances WHERE status='active' ORDER BY created_at LIMIT 1",
    ).fetchone()[0])
    rows = conn.execute(
        """SELECT id,query_hash,channel,item_id,signals_json,score,injected,reason,created_at
           FROM assistant_knowledge_retrieval_audit WHERE assistant_id=?
           ORDER BY id DESC LIMIT ?""",
        (scoped_assistant, max(1, min(int(limit), 200))),
    ).fetchall()
    return [
        {
            "id": int(row[0]), "query_hash": str(row[1]), "channel": str(row[2]),
            "item_id": str(row[3]), "signals": json.loads(str(row[4] or "{}")),
            "score": float(row[5]), "injected": bool(row[6]), "reason": str(row[7]),
            "created_at": str(row[8]),
        }
        for row in rows
    ]


def knowledge_lints(conn: sqlite3.Connection, assistant_id: str, *, limit: int = 30) -> list[dict]:
    items = [
        _row_dict(row)
        for row in conn.execute(
            "SELECT * FROM assistant_knowledge_items WHERE assistant_id=? AND status='published'",
            (assistant_id,),
        ).fetchall()
    ]
    relation_counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            """SELECT item_id,count(*) FROM (
                SELECT from_item_id AS item_id FROM assistant_knowledge_relations WHERE assistant_id=?
                UNION ALL SELECT to_item_id FROM assistant_knowledge_relations WHERE assistant_id=?
            ) GROUP BY item_id""",
            (assistant_id, assistant_id),
        ).fetchall()
    }
    result: list[dict] = []
    title_groups: dict[tuple[str, str], list[dict]] = {}
    for item in items:
        title_groups.setdefault((str(item.get("kind")), str(item.get("title")).casefold()), []).append(item)
        freshness = effective_freshness(item)
        if freshness != "fresh":
            result.append({
                "code": f"knowledge_{freshness}", "severity": "warning",
                "item_id": item["id"], "title": item["title"],
            })
        evidence = json.loads(str(item.get("evidence_refs_json") or "[]"))
        has_source = any(str(item.get(key) or "") for key in ("source_ref", "source_memory_id", "source_thread_id"))
        if not evidence and not has_source and str(item.get("source_type")) != "admin":
            result.append({
                "code": "knowledge_orphan_source", "severity": "warning",
                "item_id": item["id"], "title": item["title"],
            })
        if not relation_counts.get(str(item["id"])):
            result.append({
                "code": "knowledge_unrelated", "severity": "info",
                "item_id": item["id"], "title": item["title"],
            })
    for group in title_groups.values():
        hashes = {str(item.get("content_hash")) for item in group}
        if len(group) > 1 and len(hashes) > 1:
            for item in group:
                result.append({
                    "code": "knowledge_possible_conflict", "severity": "warning",
                    "item_id": item["id"], "title": item["title"],
                })
    return result[: max(1, min(int(limit), 100))]


def knowledge_workspace(conn: sqlite3.Connection, assistant_id: str) -> dict:
    counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            """SELECT status,count(*) FROM assistant_knowledge_items
               WHERE assistant_id=? GROUP BY status""",
            (assistant_id,),
        ).fetchall()
    }
    freshness = {status: 0 for status in sorted(FRESHNESS_STATUSES)}
    for row in conn.execute(
        "SELECT status,freshness_status,fresh_until FROM assistant_knowledge_items WHERE assistant_id=?",
        (assistant_id,),
    ).fetchall():
        freshness[effective_freshness({"status": row[0], "freshness_status": row[1], "fresh_until": row[2]})] += 1
    relation_count = int(conn.execute(
        "SELECT count(*) FROM assistant_knowledge_relations WHERE assistant_id=?",
        (assistant_id,),
    ).fetchone()[0])
    revision_count = int(conn.execute(
        "SELECT count(*) FROM assistant_knowledge_revisions WHERE assistant_id=?",
        (assistant_id,),
    ).fetchone()[0])
    audit_count = int(conn.execute(
        "SELECT count(*) FROM assistant_knowledge_retrieval_audit WHERE assistant_id=?",
        (assistant_id,),
    ).fetchone()[0])
    lints = knowledge_lints(conn, assistant_id)
    return {
        "counts": counts,
        "freshness": freshness,
        "revision_count": revision_count,
        "relation_count": relation_count,
        "retrieval_audit_count": audit_count,
        "search_backend": search_backend(conn),
        "lints": lints,
        "lint_counts": {
            "warning": sum(1 for item in lints if item["severity"] == "warning"),
            "info": sum(1 for item in lints if item["severity"] == "info"),
        },
    }


__all__ = [
    "content_hash", "effective_freshness", "knowledge_lints", "knowledge_workspace",
    "lifecycle_values", "list_knowledge_revisions", "list_retrieval_audit",
    "normalize_evidence_refs", "record_knowledge_revision", "record_retrieval_audit",
    "rebuild_knowledge_search_index", "search_projection", "source_hash", "sync_search_item",
]
