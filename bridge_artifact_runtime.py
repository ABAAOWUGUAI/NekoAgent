#!/usr/bin/env python3
"""Small Gate 7 integration façade kept outside the legacy Bridge."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Callable

from bridge_artifact_broker import ArtifactAuthorizationBroker, ArtifactBrokerClient, broker_security_supported
from bridge_artifact_cutover import artifact_cutover_plan, artifact_preview_feature_enabled
from bridge_artifact_http import ArtifactHttpApi
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
        return self.service.capture_task_manifest(
            capture_task, origin_assistant_id=str(assistant.get("id") or ""),
        )

    def _revision_task(self, artifact: dict, payload: dict) -> dict:
        instruction = str(payload.get("instruction") or "").strip()
        if not instruction or len(instruction) > 8000:
            raise ValueError("artifact_revision_instruction_invalid")
        version_id = str(artifact.get("current_version_id") or "")
        workspace = self.revision_root / (artifact["id"] + "-" + uuid.uuid4().hex[:10])
        self.service.materialize_version(
            version_id, workspace, owner_id=str(artifact.get("owner_id") or "admin"),
        )
        prompt = (
            f"修改当前成品《{artifact['title']}》。用户要求：{instruction}\n"
            f"必须在完成时生成 .agent-artifact-manifest.json，并将 artifact_id 写为 {artifact['id']}，"
            "只列出本次成品文件，不要包含缓存、依赖或凭据。"
        )
        source_task_id = ""
        with self._task_connect() as conn:
            source_run_id = str(artifact.get("source_run_id") or "")
            if not source_run_id and str(artifact.get("source_goal_id") or ""):
                row = conn.execute(
                    "SELECT current_run_id FROM goals WHERE id=?",
                    (str(artifact.get("source_goal_id")),),
                ).fetchone()
                source_run_id = str(row[0] or "") if row else ""
            if source_run_id:
                row = conn.execute("SELECT legacy_task_id FROM runs WHERE id=?", (source_run_id,)).fetchone()
                source_task_id = str(row[0] or "") if row else ""
        return self._create_task(
            prompt=prompt, sandbox="workspace-write",
            timeout=max(60, min(int(payload.get("timeout") or 600), 900)), cwd=self._safe_cwd(str(workspace)),
            source="admin", user_id="admin", origin_message=instruction,
            intent="artifact_revision", mode="work",
            source_task_id=source_task_id,
            follow_up_source_task_id=source_task_id,
            artifact_revision_id=str(artifact.get("id") or ""),
            artifact_revision_base_version_id=version_id,
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
