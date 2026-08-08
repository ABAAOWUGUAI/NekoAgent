#!/usr/bin/env python3
"""Curated shared knowledge with explicit review and channel audiences."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from bridge_living_wiki import (
    content_hash,
    effective_freshness,
    knowledge_lints,
    knowledge_workspace as living_knowledge_workspace,
    lifecycle_values,
    list_knowledge_revisions,
    list_retrieval_audit,
    rebuild_knowledge_search_index,
    record_knowledge_revision,
    record_retrieval_audit,
    search_projection,
    source_hash,
    sync_search_item,
)
from bridge_continuity_service import list_memory_candidates
from bridge_conversation_memory import list_memories, list_threads, thread_messages


AUDIENCES = {"private_all", "group_all", "all_channels"}
STATUSES = {"draft", "published", "archived", "rejected"}
KINDS = {"fact", "preference", "procedure", "reference", "decision", "lesson", "current_state"}
RELATION_TYPES = {"relates_to", "depends_on", "supersedes", "implements", "derived_from"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _assistant_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT id FROM assistant_instances WHERE status='active' ORDER BY created_at LIMIT 1").fetchone()
    if not row:
        raise ValueError("active_assistant_missing")
    return str(row[0])


def _public(row: sqlite3.Row) -> dict:
    result = {key: row[key] for key in row.keys()}
    if "tags_json" in result:
        try:
            result["tags"] = json.loads(result.pop("tags_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            result["tags"] = []
    if "evidence_refs_json" in result:
        try:
            result["evidence_refs"] = json.loads(result.pop("evidence_refs_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            result["evidence_refs"] = []
    if "freshness_status" in result:
        result["effective_freshness"] = effective_freshness(result)
    return result


def _tags(value: object) -> list[str]:
    source = value
    if isinstance(value, str) and value.lstrip().startswith("["):
        try:
            decoded = json.loads(value)
            source = decoded if isinstance(decoded, list) else value
        except json.JSONDecodeError:
            source = value
    if not isinstance(source, list):
        source = re.split(r"[,，\s]+", str(source or ""))
    result = []
    for item in source:
        tag = str(item or "").strip()[:40]
        if tag and tag not in result:
            result.append(tag)
    return result[:12]


def _keyword_set(text: str) -> set[str]:
    lowered = str(text or "").lower()
    words = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", lowered):
        words.add(chunk)
        words.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
    return words


def _keyword_candidate_where(text: str, terms: set[str]) -> tuple[str, list[object]]:
    """Bounded LIKE pre-filter so keyword overlap can run without a full scan.

    FTS trigram requires a contiguous substring match, which compound and
    natural-language queries rarely satisfy.  When FTS returns nothing we
    still must avoid both an unconditional early return (the old defect) and a
    full-corpus scan.  This narrows candidates to items sharing at least one
    query keyword in title/summary/content/tags, then ``search_published``
    scores that bounded set by overlap.
    """

    used: list[str] = []
    for term in sorted(terms):
        if len(term) < 2:
            continue
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        used.append(f"(title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR tags_json LIKE ? ESCAPE '\\')")
    if not used:
        return " AND 1=0", []
    params: list[object] = []
    for term in sorted(terms):
        if len(term) < 2:
            continue
        pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params.extend([pattern] * 4)
    return " AND (" + " OR ".join(used) + ")", params


def list_knowledge(
    conn: sqlite3.Connection,
    *,
    status: str = "",
    kind: str = "",
    query: str = "",
    limit: int = 100,
) -> list[dict]:
    assistant_id = _assistant_id(conn)
    params: list[object] = [assistant_id]
    where = "assistant_id=?"
    if status:
        if status not in STATUSES:
            raise ValueError("knowledge_status_invalid")
        where += " AND status=?"
        params.append(status)
    if kind:
        if kind not in KINDS:
            raise ValueError("knowledge_kind_invalid")
        where += " AND kind=?"
        params.append(kind)
    search = str(query or "").strip()
    projection_order: dict[str, int] = {}
    if search:
        matches = search_projection(conn, assistant_id, search, limit=200)
        if not matches:
            return []
        projection_order = {item["item_id"]: index for index, item in enumerate(matches)}
        placeholders = ",".join("?" for _ in projection_order)
        where += f" AND id IN ({placeholders})"
        params.extend(projection_order)
    params.append(max(1, min(int(limit), 200)))
    rows = conn.execute(
        f"SELECT * FROM assistant_knowledge_items WHERE {where} ORDER BY updated_at DESC LIMIT ?",
        params,
    ).fetchall()
    items = [_public(row) for row in rows]
    if not search:
        return items
    items.sort(key=lambda item: projection_order.get(str(item.get("id")), 10_000))
    return items


def _insert_knowledge(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    actor: str,
    source_type: str,
    source_ref: str,
) -> dict:
    title = str(payload.get("title") or "").strip()
    content = str(payload.get("content") or "").strip()
    audience = str(payload.get("audience") or "all_channels").strip()
    kind = str(payload.get("kind") or "fact").strip()
    if not title or len(title) > 120:
        raise ValueError("knowledge_title_invalid")
    if not content or len(content) > 12000:
        raise ValueError("knowledge_content_invalid")
    if audience not in AUDIENCES:
        raise ValueError("knowledge_audience_invalid")
    if kind not in KINDS:
        raise ValueError("knowledge_kind_invalid")
    confidence = max(0.0, min(float(payload.get("confidence") or 1.0), 1.0))
    item_id = "knowledge-" + uuid.uuid4().hex
    now = _now()
    provenance = {
        "source_type": source_type,
        "source_ref": source_ref,
        "source_memory_id": str(payload.get("source_memory_id") or ""),
        "source_thread_id": str(payload.get("source_thread_id") or ""),
        "source_scope_type": str(payload.get("source_scope_type") or ""),
        "consent_basis": str(payload.get("consent_basis") or "explicit"),
    }
    lifecycle = lifecycle_values(payload)
    conn.execute(
        """INSERT INTO assistant_knowledge_items(
            id,assistant_id,title,content,audience,status,source_type,source_ref,
            version,created_by,reviewed_by,created_at,updated_at,published_at,
            kind,summary,tags_json,confidence,source_memory_id,source_thread_id,
            source_scope_type,consent_basis,supersedes_id,review_note,content_hash,
            source_hash,evidence_refs_json,freshness_status,fresh_until,last_verified_at
        ) VALUES(?,?,?,?,?,'draft',?,?,1,?,'',?,?,'',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            item_id, _assistant_id(conn), title, content, audience, source_type, source_ref,
            actor, now, now, kind, str(payload.get("summary") or "").strip()[:500],
            json.dumps(_tags(payload.get("tags")), ensure_ascii=False), confidence,
            provenance["source_memory_id"], provenance["source_thread_id"],
            provenance["source_scope_type"], provenance["consent_basis"],
            str(payload.get("supersedes_id") or ""), str(payload.get("review_note") or "").strip()[:1000],
            content_hash(content), source_hash(provenance), lifecycle["evidence_refs_json"],
            lifecycle["freshness_status"], lifecycle["fresh_until"], lifecycle["last_verified_at"],
        ),
    )
    record_knowledge_revision(conn, item_id, operation="created", actor=actor)
    sync_search_item(conn, item_id)
    conn.commit()
    return _public(conn.execute("SELECT * FROM assistant_knowledge_items WHERE id=?", (item_id,)).fetchone())


def create_knowledge(conn: sqlite3.Connection, payload: dict, *, actor: str = "admin") -> dict:
    return _insert_knowledge(conn, payload, actor=actor, source_type="admin", source_ref="")


def update_knowledge(conn: sqlite3.Connection, item_id: str, payload: dict, *, actor: str = "admin") -> dict:
    current = conn.execute(
        "SELECT * FROM assistant_knowledge_items WHERE id=? AND assistant_id=?",
        (item_id, _assistant_id(conn)),
    ).fetchone()
    if not current:
        raise ValueError("knowledge_not_found")
    if str(current["status"]) != "draft":
        raise ValueError("knowledge_edit_requires_draft")
    expected = int(payload.get("expected_version") or 0)
    title = str(payload.get("title", current["title"])).strip()
    content = str(payload.get("content", current["content"])).strip()
    audience = str(payload.get("audience", current["audience"])).strip()
    kind = str(payload.get("kind", current["kind"])).strip()
    if not title or len(title) > 120 or not content or len(content) > 12000:
        raise ValueError("knowledge_content_invalid")
    if audience not in AUDIENCES or kind not in KINDS:
        raise ValueError("knowledge_classification_invalid")
    lifecycle = lifecycle_values(payload, current=dict(current))
    changed = conn.execute(
        """UPDATE assistant_knowledge_items SET title=?,content=?,audience=?,kind=?,summary=?,
            tags_json=?,review_note=?,content_hash=?,source_hash=?,evidence_refs_json=?,
            freshness_status=?,fresh_until=?,last_verified_at=?,updated_at=?,version=version+1
            WHERE id=? AND assistant_id=? AND version=?""",
        (
            title, content, audience, kind, str(payload.get("summary", current["summary"])).strip()[:500],
            json.dumps(_tags(payload.get("tags", current["tags_json"])), ensure_ascii=False),
            str(payload.get("review_note", current["review_note"])).strip()[:1000],
            content_hash(content), source_hash(dict(current)), lifecycle["evidence_refs_json"],
            lifecycle["freshness_status"], lifecycle["fresh_until"], lifecycle["last_verified_at"],
            _now(), item_id,
            _assistant_id(conn), expected,
        ),
    ).rowcount
    if not changed:
        raise ValueError("knowledge_version_conflict")
    record_knowledge_revision(conn, item_id, operation="edited", actor=actor)
    sync_search_item(conn, item_id)
    conn.commit()
    return _public(conn.execute("SELECT * FROM assistant_knowledge_items WHERE id=?", (item_id,)).fetchone())


def promote_memory(conn: sqlite3.Connection, memory_id: str, payload: dict, *, actor: str = "admin") -> dict:
    memory = conn.execute(
        "SELECT * FROM memory_records WHERE id=? AND assistant_id=?",
        (memory_id, _assistant_id(conn)),
    ).fetchone()
    if not memory or str(memory["status"]) != "active":
        raise ValueError("memory_not_found")
    if str(memory["sensitivity"]) == "sensitive" or str(memory["scope_type"]) == "sensitive_private":
        raise ValueError("sensitive_memory_cannot_be_promoted")
    scope = str(memory["scope_type"])
    if scope in {"thread", "qq_group", "project"} and not bool(payload.get("confirm_scope_expansion")):
        raise ValueError("memory_scope_expansion_confirmation_required")
    content = str(payload.get("content") or memory["content"]).strip()
    title = str(payload.get("title") or f"由记忆整理：{content[:32]}").strip()
    draft = dict(payload)
    draft.update(
        {
            "title": title,
            "content": content,
            "source_memory_id": memory_id,
            "source_thread_id": str(memory["source_thread_id"] or ""),
            "source_scope_type": scope,
            "consent_basis": str(memory["consent_basis"] or "explicit"),
            "confidence": max(0.0, min(int(memory["score"] or 5) / 10.0, 1.0)),
        },
    )
    return _insert_knowledge(conn, draft, actor=actor, source_type="memory", source_ref=memory_id)


def review_knowledge(conn: sqlite3.Connection, item_id: str, payload: dict, *, actor: str = "admin") -> dict:
    status = str(payload.get("status") or "").strip()
    if status not in {"published", "archived", "rejected"}:
        raise ValueError("knowledge_review_status_invalid")
    expected = int(payload.get("expected_version") or 0)
    current = conn.execute(
        "SELECT * FROM assistant_knowledge_items WHERE id=? AND assistant_id=?",
        (item_id, _assistant_id(conn)),
    ).fetchone()
    if not current:
        raise ValueError("knowledge_not_found")
    now = _now()
    published_at = now if status == "published" else ""
    review_payload = dict(payload)
    if status in {"archived", "rejected"} and "freshness_status" not in review_payload:
        review_payload["freshness_status"] = "expired"
    lifecycle = lifecycle_values(
        review_payload,
        current=dict(current),
        published=status == "published",
    )
    changed = conn.execute(
        """UPDATE assistant_knowledge_items SET status=?,reviewed_by=?,review_note=?,updated_at=?,
            published_at=?,evidence_refs_json=?,freshness_status=?,fresh_until=?,last_verified_at=?,
            version=version+1 WHERE id=? AND assistant_id=? AND version=?""",
        (
            status, actor, str(payload.get("review_note") or "")[:1000], now, published_at,
            lifecycle["evidence_refs_json"], lifecycle["freshness_status"],
            lifecycle["fresh_until"], lifecycle["last_verified_at"], item_id,
            _assistant_id(conn), expected,
        ),
    ).rowcount
    if not changed:
        exists = conn.execute("SELECT 1 FROM assistant_knowledge_items WHERE id=?", (item_id,)).fetchone()
        raise ValueError("knowledge_version_conflict" if exists else "knowledge_not_found")
    record_knowledge_revision(conn, item_id, operation=status, actor=actor)
    sync_search_item(conn, item_id)
    conn.commit()
    return _public(conn.execute("SELECT * FROM assistant_knowledge_items WHERE id=?", (item_id,)).fetchone())


def search_published(conn: sqlite3.Connection, text: str, *, channel: str, limit: int = 5) -> list[dict]:
    audience = "group_all" if channel == "group" else "private_all"
    terms = _keyword_set(text)
    assistant_id = _assistant_id(conn)
    projected = search_projection(conn, assistant_id, text, limit=100) if str(text or "").strip() else []
    projection = {item["item_id"]: item for item in projected}
    if str(text or "").strip() and not projection and not terms:
        record_retrieval_audit(
            conn,
            assistant_id=assistant_id,
            query=text,
            channel=channel,
            item_id="",
            signals={"backend": "none", "audience": audience},
            score=0,
            injected=False,
            reason="no_match",
        )
        conn.commit()
        return []
    params: list[object] = [assistant_id, audience]
    where = "assistant_id=? AND status='published' AND audience IN (?, 'all_channels')"
    if projection:
        where += " AND id IN (" + ",".join("?" for _ in projection) + ")"
        params.extend(projection)
    else:
        # FTS produced no projection: fall back to a bounded keyword candidate
        # set so natural-language / compound queries can still be scored by
        # overlap without scanning the whole published corpus.
        keyword_where, keyword_params = _keyword_candidate_where(text, terms)
        where += keyword_where
        params.extend(keyword_params)
    params.append(100)
    rows = conn.execute(
        f"""SELECT * FROM assistant_knowledge_items WHERE {where}
            ORDER BY updated_at DESC LIMIT ?""",
        params,
    ).fetchall()
    scored = []
    for row in rows:
        item = _public(row)
        haystack = " ".join((str(item.get("title") or ""), str(item.get("summary") or ""), str(item.get("content") or ""), " ".join(item.get("tags") or [])))
        overlap = len(terms & _keyword_set(haystack))
        freshness = item.get("effective_freshness") or "unverified"
        if freshness == "expired":
            continue
        projected_item = projection.get(str(item.get("id")))
        if overlap or projected_item:
            freshness_bonus = {"fresh": 2.0, "unverified": 0.0, "stale": -1.0}.get(freshness, 0.0)
            projection_bonus = 3.0 if projected_item else 0.0
            score = overlap * 10.0 + projection_bonus + freshness_bonus
            reason = "fts_bm25" if projected_item else "keyword_overlap" if overlap else "recent_published"
            backend = (projected_item or {}).get("backend") or reason
            item["retrieval"] = {
                "reason": reason,
                "score": score,
                "freshness": freshness,
                "backend": backend,
            }
            scored.append((score, item))
    scored.sort(key=lambda item: (item[0], str(item[1]["updated_at"])), reverse=True)
    selected = [item for _, item in scored[:max(1, min(int(limit), 10))]]
    for item in selected:
        retrieval = item["retrieval"]
        record_retrieval_audit(
            conn,
            assistant_id=assistant_id,
            query=text,
            channel=channel,
            item_id=str(item["id"]),
            signals={
                "backend": retrieval["backend"],
                "freshness": retrieval["freshness"],
                "audience": str(item.get("audience") or ""),
            },
            score=float(retrieval["score"]),
            injected=True,
            reason=str(retrieval["reason"]),
        )
    if not selected:
        if projection or rows:
            reason = "no_authorized_match"
        else:
            reason = "no_match"
        record_retrieval_audit(
            conn,
            assistant_id=assistant_id,
            query=text,
            channel=channel,
            item_id="",
            signals={
                "backend": "filtered" if reason == "no_authorized_match" else "none",
                "audience": audience,
            },
            score=0,
            injected=False,
            reason=reason,
        )
    conn.commit()
    return selected


def create_relation(conn: sqlite3.Connection, payload: dict, *, actor: str = "admin") -> dict:
    assistant_id = _assistant_id(conn)
    from_id = str(payload.get("from_item_id") or "").strip()
    to_id = str(payload.get("to_item_id") or "").strip()
    relation = str(payload.get("relation_type") or "relates_to").strip()
    if not from_id or not to_id or from_id == to_id or relation not in RELATION_TYPES:
        raise ValueError("knowledge_relation_invalid")
    count = conn.execute(
        "SELECT COUNT(*) FROM assistant_knowledge_items WHERE assistant_id=? AND id IN (?,?)",
        (assistant_id, from_id, to_id),
    ).fetchone()[0]
    if int(count) != 2:
        raise ValueError("knowledge_relation_item_not_found")
    relation_id = "knowledge-relation-" + uuid.uuid4().hex
    conn.execute(
        """INSERT INTO assistant_knowledge_relations(
            id,assistant_id,from_item_id,to_item_id,relation_type,created_by,created_at
        ) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(assistant_id,from_item_id,to_item_id,relation_type) DO NOTHING""",
        (relation_id, assistant_id, from_id, to_id, relation, actor, _now()),
    )
    conn.commit()
    row = conn.execute(
        """SELECT * FROM assistant_knowledge_relations
        WHERE assistant_id=? AND from_item_id=? AND to_item_id=? AND relation_type=?""",
        (assistant_id, from_id, to_id, relation),
    ).fetchone()
    return _public(row)


def list_relations(conn: sqlite3.Connection, *, item_id: str = "") -> list[dict]:
    assistant_id = _assistant_id(conn)
    if item_id:
        rows = conn.execute(
            """SELECT * FROM assistant_knowledge_relations
            WHERE assistant_id=? AND (from_item_id=? OR to_item_id=?) ORDER BY created_at DESC""",
            (assistant_id, item_id, item_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM assistant_knowledge_relations WHERE assistant_id=? ORDER BY created_at DESC LIMIT 500",
            (assistant_id,),
        ).fetchall()
    return [_public(row) for row in rows]


def get_knowledge_workspace(conn: sqlite3.Connection) -> dict:
    assistant_id = _assistant_id(conn)
    result = living_knowledge_workspace(conn, assistant_id)
    # This endpoint is a read-only product projection. Keeping the first useful
    # Knowledge screen in one SQLite snapshot avoids six remote round trips
    # while the underlying domain tables remain the authorities.
    result["items"] = list_knowledge(conn, limit=100)
    result["memory_candidates"] = list_memory_candidates(conn, status="pending", limit=100)
    result["memories"] = list_memories(conn, limit=20, owner_management=True)
    # C4: promotable memories are active, non-sensitive records that an Owner
    # may explicitly turn into a Knowledge Draft.  This is a read-only
    # projection; promotion still requires ``promote_memory`` with scope
    # confirmation.
    try:
        result["promotable_memories"] = [
            dict(row)
            for row in conn.execute(
                """SELECT id,content,scope_type,sensitivity,consent_basis,updated_at
                   FROM memory_records
                   WHERE status='active' AND sensitivity<>'sensitive'
                         AND scope_type NOT IN ('sensitive_private','global_preference')
                   ORDER BY updated_at DESC LIMIT 50"""
            ).fetchall()
        ]
    except sqlite3.Error:
        result["promotable_memories"] = []
    result["conversation_threads"] = list_threads(conn, limit=50)
    result["recent_messages"] = []
    if result["conversation_threads"]:
        result["recent_messages"] = thread_messages(
            conn,
            str(result["conversation_threads"][0]["id"]),
            limit=20,
        )
    current = conn.execute(
        """SELECT p.id,p.name,p.path,p.description,p.updated_at
           FROM settings s JOIN projects p ON p.id=s.value
           WHERE s.key='current_project_id' AND p.active=1 LIMIT 1""",
    ).fetchone()
    result["current_project"] = (
        {key: current[key] for key in current.keys()} if current else None
    )
    return result


def get_knowledge_revisions(conn: sqlite3.Connection, *, item_id: str, limit: int = 50) -> list[dict]:
    return list_knowledge_revisions(
        conn,
        assistant_id=_assistant_id(conn),
        item_id=item_id,
        limit=limit,
    )


def get_retrieval_audit(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict]:
    return list_retrieval_audit(conn, assistant_id=_assistant_id(conn), limit=limit)


def get_knowledge_lints(conn: sqlite3.Connection, *, limit: int = 30) -> list[dict]:
    return knowledge_lints(conn, _assistant_id(conn), limit=limit)


__all__ = [
    "create_knowledge", "create_relation", "get_knowledge_lints", "get_knowledge_revisions",
    "get_knowledge_workspace", "get_retrieval_audit", "list_knowledge", "list_relations",
    "promote_memory", "rebuild_knowledge_search_index", "review_knowledge",
    "search_published", "update_knowledge",
]
