#!/usr/bin/env python3
"""Runtime adapters for evidence presentation and Automation references."""

from __future__ import annotations

import json
from collections.abc import Callable

from bridge_automation_contracts import (
    build_github_purpose_prompt,
    delivery_text_sha256,
    output_contract_from_job,
    output_contract_hash,
    parse_github_purpose_summaries,
    present_github_trending,
)


def github_purpose_summaries(
    job: dict,
    items: list[dict],
    *,
    settings: dict,
    model_settings: dict,
    call_openai_retry: Callable,
    call_openai: Callable,
    run_codex: Callable,
    record_model: Callable,
    default_cwd: str,
) -> dict[str, str]:
    """Translate capability descriptions only; failures remain unavailable."""

    if not any(str(item.get("description") or "").strip() for item in items if isinstance(item, dict)):
        return {}
    del settings  # The resolved role settings are the only runtime authority used here.
    messages = build_github_purpose_prompt(items)
    provider = str(model_settings.get("chat_provider") or "codex")
    try:
        if provider == "openai-compatible":
            prepared = {**model_settings, "chat_temperature": "0", "chat_max_tokens": "1800"}
            result = call_openai_retry(
                prepared,
                messages,
                timeout=60,
                user_id=str(job.get("user_id") or ""),
                call_model=call_openai,
                record_model=record_model,
                empty_source="automation_presenter_empty_initial",
                retry_instruction="输出协议：必须输出非空 JSON；不得补充来源描述之外的事实。",
            )
        else:
            result = run_codex(
                "\n\n".join(item["content"] for item in messages),
                cwd=default_cwd,
                timeout=120,
                settings_override=model_settings,
            )
            record_model(
                model_settings,
                result,
                source="automation_result_presenter",
                user_id=str(job.get("user_id") or ""),
            )
        if not result.get("ok"):
            return {}
        return parse_github_purpose_summaries(result.get("reply") or result.get("output") or "", items)
    except Exception:
        return {}


def resolve_automation_target(
    actor_id: str,
    inbound_context: dict,
    *,
    outbox: object,
    assistant_connect: Callable,
) -> dict:
    """Resolve one confirmed quoted Delivery to its Automation without chat text."""

    reply_id = str(inbound_context.get("reply_to_external_message_id") or "").strip()
    reply_digest = str(inbound_context.get("reply_text_sha256") or "").strip().lower()
    matches: dict[str, dict] = {}
    with outbox._connect() as conn:
        rows = conn.execute(
            """SELECT payload_json,platform_message_id FROM delivery_outbox
               WHERE channel='qq' AND destination=? AND acked_at<>''
                 AND delivery_certainty='confirmed'
               ORDER BY updated_at DESC LIMIT 80""",
            (str(actor_id or ""),),
        ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        job_id = str(payload.get("automation_job_id") or "").strip()
        platform_id = str(row["platform_message_id"] or "").strip()
        id_match = bool(reply_id and platform_id and (reply_id == platform_id or reply_id.endswith(":" + platform_id)))
        digest_match = bool(
            reply_digest and payload.get("content")
            and delivery_text_sha256(payload.get("content")) == reply_digest
        )
        if job_id and (id_match or digest_match):
            matches[job_id] = {
                "job_id": job_id,
                "reference": "platform_message" if id_match else "quoted_content_digest",
            }
    with assistant_connect() as conn:
        if len(matches) == 1:
            target = next(iter(matches.values()))
            job = conn.execute(
                "SELECT id,revision FROM automation_jobs WHERE id=? AND user_id=? AND enabled=1",
                (target["job_id"], str(actor_id or "")),
            ).fetchone()
            if job:
                return {"status": "resolved", "job_id": str(job["id"]), "revision": int(job["revision"]), **target}
        if reply_id or reply_digest:
            return {"status": "not_found", "reason": "quoted_delivery_not_resolved"}
        jobs = conn.execute(
            "SELECT id,revision FROM automation_jobs WHERE user_id=? AND enabled=1 ORDER BY updated_at DESC LIMIT 2",
            (str(actor_id or ""),),
        ).fetchall()
    if len(jobs) == 1:
        return {
            "status": "resolved", "job_id": str(jobs[0]["id"]),
            "revision": int(jobs[0]["revision"]), "reference": "only_enabled_job",
        }
    return {"status": "ambiguous" if jobs else "not_found", "reason": "target_not_unique"}


def prepare_github_delivery_payload(
    job: dict,
    light_result: dict,
    github_arguments: dict,
    purpose_summaries: dict[str, str],
    *,
    assistant_connect: Callable,
    reserve_items: Callable,
) -> dict:
    """Build one evidence-backed Artifact and reserve its dedupe item keys."""

    evidence = light_result.get("evidence") if isinstance(light_result.get("evidence"), list) else []
    output = light_result.get("output") if isinstance(light_result.get("output"), dict) else {}
    items = output.get("items") if isinstance(output.get("items"), list) else []
    contract = output_contract_from_job(job)
    contract_hash = output_contract_hash(contract)
    if str(job.get("output_contract_hash") or "") not in {"", contract_hash}:
        raise RuntimeError("automation_output_contract_snapshot_mismatch")
    presentation = present_github_trending(light_result, contract, purpose_summaries=purpose_summaries)
    content = str(presentation.get("content") or "").strip()
    if not content or not evidence:
        raise RuntimeError("automation_evidence_or_presentation_missing")
    item_keys = [
        str(item.get("repo") or item.get("name") or "").strip()
        for item in items if isinstance(item, dict)
    ]
    if len([key for key in item_keys if key]) < int(github_arguments.get("limit") or 10):
        raise RuntimeError("github_trending_insufficient_unique_results")
    with assistant_connect() as conn:
        reserve_items(conn, job_id=str(job.get("id") or ""), run_id=str(job.get("run_id") or ""), item_keys=item_keys)
    return {
        "kind": "automation_result", "automation_job_id": job["id"],
        "automation_run_id": job["run_id"], "user_id": job["user_id"],
        "capability_id": "github.trending.read", "evidence": evidence, "content": content,
        "artifact": presentation.get("artifact") or {},
        "job_revision": int(job.get("revision") or job.get("job_revision") or 1),
        "output_contract_hash": contract_hash,
    }


__all__ = ["github_purpose_summaries", "prepare_github_delivery_payload", "resolve_automation_target"]
