#!/usr/bin/env python3
"""Small Gate 7 integration façade kept outside the legacy Bridge."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from bridge_artifact_broker import ArtifactAuthorizationBroker, ArtifactBrokerClient, broker_security_supported
from bridge_artifact_cutover import artifact_cutover_plan, artifact_preview_feature_enabled
from bridge_artifact_http import ArtifactHttpApi
from bridge_artifact_repository import ArtifactRepository
from bridge_artifact_service import ARTIFACT_MANIFEST_INSTRUCTION, ArtifactService
from bridge_assistant_identity import current_assistant


class ArtifactRuntime:
    def __init__(
        self,
        assistant_connect: Callable,
        task_connect: Callable,
        json_response: Callable,
        create_task: Callable,
        safe_cwd: Callable,
    ) -> None:
        self._assistant_connect = assistant_connect
        self._task_connect = task_connect
        self._create_task = create_task
        self._safe_cwd = safe_cwd
        self.storage_root = Path(os.environ.get("ARTIFACT_STORAGE_ROOT", "/var/lib/agent-artifacts"))
        self.socket_path = Path(os.environ.get("ARTIFACT_BROKER_SOCKET", "/run/agent-artifact/broker.sock"))
        self.preview_base_url = os.environ.get("ARTIFACT_PREVIEW_BASE_URL", "")
        self.redemption_base_url = os.environ.get("ARTIFACT_REDEMPTION_BASE_URL", "").rstrip("/")
        self.admin_origin = os.environ.get("ADMIN_ORIGIN", "")
        self.revision_root = Path(os.environ.get("ARTIFACT_REVISION_ROOT", "/opt/agent-workspace/artifact-revisions"))
        self.preview_uid = int(os.environ.get("ARTIFACT_PREVIEW_UID", "-1"))
        preview_gid = int(os.environ.get("ARTIFACT_PREVIEW_GID", "-1"))
        self.service = ArtifactService(task_connect, self.storage_root)
        self.client = ArtifactBrokerClient(self.socket_path)
        self.broker = ArtifactAuthorizationBroker(
            task_connect, self.socket_path, allowed_uid=self.preview_uid,
            socket_gid=preview_gid if preview_gid >= 0 else None,
            feature_enabled=self.enabled,
        ) if self.preview_uid >= 0 and broker_security_supported() else None
        self.api = ArtifactHttpApi(
            assistant_connect, task_connect, json_response, self.service,
            preview_base_url=self.preview_base_url, revision_task=self._revision_task,
            cutover_plan=self.cutover_plan,
        )

    def enabled(self) -> bool:
        with self._assistant_connect() as conn:
            return artifact_preview_feature_enabled(conn)

    @staticmethod
    def retry_source_prompt(prompt: str) -> str:
        """Remove a previous Task-owned manifest suffix before a retry."""

        text = str(prompt or "")
        marker = "\n\n" + ARTIFACT_MANIFEST_INSTRUCTION
        index = text.find(marker)
        return text[:index].rstrip() if index >= 0 else text

    def decorate_prompt(
        self,
        prompt: str,
        sandbox: str,
        *,
        task_id: str = "",
        created_at: str = "",
    ) -> str:
        if sandbox != "workspace-write" or not self.enabled():
            return prompt
        binding = (
            f"本任务的成品清单必须写入 task_id={task_id}，generated_at 必须是不早于 {created_at} 的当前 ISO-8601 时间。"
        )
        return str(prompt).rstrip() + "\n\n" + ARTIFACT_MANIFEST_INSTRUCTION + "\n" + binding

    def capture(self, task: dict) -> dict | None:
        if not self.enabled() or str(task.get("sandbox") or "") != "workspace-write":
            return None
        with self._assistant_connect() as conn:
            assistant = current_assistant(conn) or {}
        capture_task = dict(task)
        capture_task["user_id"] = str(assistant.get("owner_actor_id") or "admin")
        captured = self.service.capture_task_manifest(
            capture_task, origin_assistant_id=str(assistant.get("id") or ""),
        )
        if not captured or str(task.get("source") or "") != "qq" or not self.redemption_base_url:
            return captured
        parsed = urlsplit(self.redemption_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("artifact_redemption_base_url_invalid")
        artifact = captured.get("artifact") if isinstance(captured.get("artifact"), dict) else {}
        version = captured.get("version") if isinstance(captured.get("version"), dict) else {}
        version_id = str(version.get("id") or "")
        if not version_id:
            raise ValueError("artifact_delivery_version_missing")
        with self._task_connect() as conn:
            grant = ArtifactRepository(conn).create_delivery_grant(
                version_id,
                created_by=str(assistant.get("owner_actor_id") or "admin"),
            )
        result = dict(captured)
        result["delivery_access"] = {
            "artifact_id": str(artifact.get("id") or ""),
            "artifact_version_id": version_id,
            "source_goal_id": str(artifact.get("source_goal_id") or ""),
            "source_run_id": str(artifact.get("source_run_id") or ""),
            "grant_id": str(grant["id"]),
            "expires_at": str(grant["expires_at"]),
            "redemption_url": self.redemption_base_url + "/r/" + str(grant["token"]),
        }
        return result

    def _revision_task(self, artifact: dict, payload: dict) -> dict:
        return self._create_revision_task(
            artifact_id=str(artifact.get("id") or ""),
            artifact_title=str(artifact.get("title") or ""),
            artifact_kind=str(artifact.get("kind") or "file"),
            base_version_id=str(artifact.get("current_version_id") or ""),
            source_goal_id=str(artifact.get("source_goal_id") or ""),
            source_run_id=str(artifact.get("source_run_id") or ""),
            instruction=str(payload.get("instruction") or ""),
            source="admin",
            user_id="admin",
            delivery_recipient_id="",
            delivery_session="",
            trace_id="",
            timeout=int(payload.get("timeout") or 600),
        )

    def latest_delivered_artifact(self, actor_id: str, channel: str, conversation_ref: str) -> dict:
        """Resolve the latest acknowledged Artifact from structured Outbox data.

        The search deliberately parses structured JSON rows after SQLite filters
        delivery success.  It never uses payload text matching and never
        introduces a second latest-artifact cache.
        """

        actor_id = str(actor_id or "")
        channel = str(channel or "")
        conversation_ref = str(conversation_ref or "")
        if not actor_id or not channel or not conversation_ref:
            return {"resolution": "not_found"}
        candidates: list[dict] = []
        with self._task_connect() as conn:
            rows = conn.execute(
                """
                SELECT id,acked_at,payload_json FROM delivery_outbox
                WHERE acked_at<>''
                ORDER BY acked_at DESC,id DESC
                """,
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"] or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            delivery = payload.get("artifact_delivery") if isinstance(payload, dict) else None
            if not isinstance(delivery, dict) or str(payload.get("kind") or "") != "run_result":
                continue
            scope = (actor_id, channel, conversation_ref)
            if (
                tuple(str(payload.get(key) or "") for key in ("actor_id", "channel", "conversation_ref")) != scope
                or tuple(str(delivery.get(key) or "") for key in ("actor_id", "channel", "conversation_ref")) != scope
            ):
                continue
            fields = {
                key: str(delivery.get(key) or "")
                for key in ("artifact_id", "artifact_version_id", "source_goal_id", "source_run_id", "grant_id")
            }
            if not all(fields.values()):
                continue
            candidates.append({"acked_at": str(row["acked_at"] or ""), **fields})
        if not candidates:
            return {"resolution": "not_found"}
        latest_at = candidates[0]["acked_at"]
        latest = [item for item in candidates if item["acked_at"] == latest_at]
        if len(latest) != 1:
            return {"resolution": "ambiguous", "candidate_count": len(latest)}
        return {"resolution": "resolved", **latest[0]}

    @staticmethod
    def delivery_revision_clarification(target: dict) -> str:
        if str(target.get("resolution") or "") == "ambiguous":
            return "我这里有不止一个刚交付的成果，怕改错。你想改哪一份？"
        return "我没有找到刚刚成功交付的成果。请先把需要修改的那份发给我，或说明它的内容。"

    def create_delivered_revision_task(
        self,
        target: dict,
        *,
        instruction: str,
        actor_id: str,
        channel: str,
        conversation_ref: str,
        delivery_session: str,
        trace_id: str,
        timeout: int,
    ) -> dict:
        if str(target.get("resolution") or "") != "resolved":
            raise ValueError("artifact_delivery_revision_target_invalid")
        artifact_id = str(target.get("artifact_id") or "")
        base_version_id = str(target.get("artifact_version_id") or "")
        with self._assistant_connect() as conn:
            assistant = current_assistant(conn) or {}
        owner_id = str(assistant.get("owner_actor_id") or "")
        if not owner_id:
            raise ValueError("artifact_delivery_revision_target_invalid")
        with self._task_connect() as conn:
            artifact = ArtifactRepository(conn).get_artifact(artifact_id)
        if not artifact or str(artifact.get("owner_id") or "") != owner_id:
            raise ValueError("artifact_delivery_revision_target_invalid")
        return self._create_revision_task(
            artifact_id=artifact_id,
            artifact_title=str(artifact.get("title") or ""),
            artifact_kind=str(artifact.get("kind") or "file"),
            base_version_id=base_version_id,
            source_goal_id=str(target.get("source_goal_id") or ""),
            source_run_id=str(target.get("source_run_id") or ""),
            instruction=instruction,
            source="qq",
            user_id=str(actor_id),
            delivery_recipient_id=str(conversation_ref),
            delivery_session=str(delivery_session),
            trace_id=str(trace_id),
            timeout=int(timeout),
            owner_id=owner_id,
        )

    def _create_revision_task(
        self,
        *,
        artifact_id: str,
        artifact_title: str,
        artifact_kind: str,
        base_version_id: str,
        source_goal_id: str,
        source_run_id: str,
        instruction: str,
        source: str,
        user_id: str,
        delivery_recipient_id: str,
        delivery_session: str,
        trace_id: str,
        timeout: int,
        owner_id: str = "admin",
    ) -> dict:
        instruction = str(instruction or "").strip()
        if not instruction or len(instruction) > 8000:
            raise ValueError("artifact_revision_instruction_invalid")
        if not artifact_id or not base_version_id:
            raise ValueError("artifact_revision_target_invalid")
        workspace = self.revision_root / (artifact_id + "-" + uuid.uuid4().hex[:10])
        self.service.materialize_version(
            base_version_id, workspace, owner_id=owner_id,
        )
        prompt = (
            f"修改当前成品《{artifact_title or '当前成品'}》。用户要求：{instruction}\n"
            f"必须在完成时生成 .agent-artifact-manifest.json，并将 artifact_id 写为 {artifact_id}，"
            f"title 必须保持为《{artifact_title or '当前成品'}》，kind 必须保持为 {artifact_kind}，"
            "只列出本次成品文件，不要包含缓存、依赖或凭据。"
        )
        source_task_id = ""
        with self._task_connect() as conn:
            source_run_id = str(source_run_id or "")
            if not source_run_id and str(source_goal_id or ""):
                row = conn.execute(
                    "SELECT current_run_id FROM goals WHERE id=?",
                    (str(source_goal_id),),
                ).fetchone()
                source_run_id = str(row[0] or "") if row else ""
            if source_run_id:
                row = conn.execute("SELECT legacy_task_id FROM runs WHERE id=?", (source_run_id,)).fetchone()
                source_task_id = str(row[0] or "") if row else ""
        return self._create_task(
            prompt=prompt, sandbox="workspace-write",
            timeout=max(60, min(int(timeout or 600), 900)), cwd=self._safe_cwd(str(workspace)),
            source=source, user_id=user_id, trace_id=trace_id, origin_message=instruction,
            intent="artifact_revision", mode="work",
            source_task_id=source_task_id,
            follow_up_source_task_id=source_task_id,
            delivery_recipient_id=delivery_recipient_id,
            delivery_session=delivery_session,
            artifact_revision_id=artifact_id,
            artifact_revision_base_version_id=base_version_id,
        )

    def start(self) -> dict:
        self.service.ensure_storage()
        reconciled = self.service.reconcile()
        if self.broker:
            self.broker.start()
        # Reconcile marks broken individual artifacts invalid/quarantined and
        # reports them in ``failed``. That is a completed recovery action, not
        # a reason to crash the whole Bridge and rely on a second restart.
        if self.enabled() and not self.broker:
            raise RuntimeError("artifact_runtime_prerequisite_failed")
        return reconciled

    def cutover_plan(self) -> dict:
        with self._assistant_connect() as assistant_conn, self._task_connect() as task_conn:
            return artifact_cutover_plan(
                assistant_conn, task_conn, storage_reconcile=self.service.reconcile,
                broker_probe=(
                    self.broker.health if self.broker else
                    lambda: {"ok": False, "service": "artifact-authorization-broker", "security": "unsupported"}
                ),
                preview_base_url=self.preview_base_url, admin_origin=self.admin_origin,
                admin_cookie_secure=os.environ.get("ADMIN_COOKIE_SECURE", "0").lower() in {"1", "true", "yes", "on"},
                tailscale_service_verified=os.environ.get("ARTIFACT_TAILSCALE_SERVICE_VERIFIED", "0").lower() in {"1", "true", "yes", "on"},
            )


__all__ = ["ArtifactRuntime"]
