from __future__ import annotations

import http.client
import json
import sqlite3
import subprocess
import zipfile
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _connect(path: Path):
    def connect():
        conn = sqlite3.connect(path, timeout=3)
        conn.row_factory = sqlite3.Row
        return conn
    return connect


class WebDispatchReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.assistant_path = Path(self.directory.name) / "assistant.sqlite3"
        self.connect = _connect(self.assistant_path)
        from bridge_reliability_schema import apply_task_message_reliability_v2

        with self.connect() as conn:
            conn.execute(
                "CREATE TABLE assistant_feature_flags(name TEXT PRIMARY KEY, enabled INTEGER NOT NULL, updated_at TEXT NOT NULL)"
            )
            apply_task_message_reliability_v2(conn)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_required_web_receipt_is_safe_while_qq_reliability_flag_is_off(self) -> None:
        from bridge_inbound_idempotency import execute_once

        calls = []
        payload = {"source": "web-console", "user_id": "web-console", "message": "整理项目", "force": "auto"}

        first = execute_once(
            self.connect, "web:session-a:request-1", "web:session-a", "web-console", payload,
            lambda: calls.append("task") or {"ok": True, "dispatch": "task", "task": {"id": "task-1"}},
            require_receipt=True,
        )
        replay = execute_once(
            self.connect, "web:session-a:request-1", "web:session-a", "web-console", payload,
            lambda: calls.append("duplicate") or {"ok": True},
            require_receipt=True,
        )

        self.assertEqual(["task"], calls)
        self.assertEqual(first, replay)

    def test_required_web_receipt_fails_closed_when_its_schema_is_unavailable(self) -> None:
        from bridge_inbound_idempotency import InboundIdempotencyUnavailableError, execute_once

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "assistant.sqlite3"

            def missing_schema_connect():
                conn = sqlite3.connect(path)
                conn.row_factory = sqlite3.Row
                return conn

            conn = missing_schema_connect()
            try:
                conn.execute(
                    "CREATE TABLE assistant_feature_flags(name TEXT PRIMARY KEY, enabled INTEGER NOT NULL, updated_at TEXT NOT NULL)"
                )
                conn.commit()
            finally:
                conn.close()
            calls: list[str] = []
            with self.assertRaisesRegex(InboundIdempotencyUnavailableError, "web_dispatch_idempotency_unavailable"):
                execute_once(
                    missing_schema_connect, "web:schema-missing", "web:session", "web-console",
                    {"source": "web-console", "message": "schema must fail closed"},
                    lambda: calls.append("must-not-run") or {"ok": True}, require_receipt=True,
                )
            self.assertEqual([], calls)

    def test_same_request_id_with_different_canonical_payload_conflicts(self) -> None:
        from bridge_inbound_idempotency import InboundConflictError, execute_once

        payload_a = {"source": "web-console", "user_id": "web-console", "message": "任务 A", "force": "auto"}
        payload_b = {**payload_a, "message": "任务 B"}
        execute_once(
            self.connect, "web:session-a:request-2", "web:session-a", "web-console", payload_a,
            lambda: {"ok": True, "dispatch": "task"}, require_receipt=True,
        )
        with self.assertRaisesRegex(InboundConflictError, "web_dispatch_request_id_payload_conflict"):
            execute_once(
                self.connect, "web:session-a:request-2", "web:session-a", "web-console", payload_b,
                lambda: self.fail("payload conflict must not execute"), require_receipt=True,
            )

    def test_processing_and_expired_web_receipts_never_reexecute_the_operation(self) -> None:
        from bridge_inbound_idempotency import (
            InboundOutcomeUnknownError,
            InboundProcessingError,
            begin_receipt,
            execute_once,
        )

        payload = {"source": "web-console", "user_id": "web-console", "message": "整理项目", "force": "auto"}
        begin_receipt(
            self.connect, "web:session-a:request-3", "web:session-a", "web-console", payload,
            require_receipt=True,
        )
        with self.assertRaisesRegex(InboundProcessingError, "web_dispatch_processing"):
            execute_once(
                self.connect, "web:session-a:request-3", "web:session-a", "web-console", payload,
                lambda: self.fail("processing receipt must not reexecute"), require_receipt=True,
            )
        with self.connect() as conn:
            conn.execute("UPDATE qq_inbound_receipts SET lease_until='2000-01-01T00:00:00+00:00'")
        with self.assertRaisesRegex(InboundOutcomeUnknownError, "web_dispatch_outcome_unknown"):
            execute_once(
                self.connect, "web:session-a:request-3", "web:session-a", "web-console", payload,
                lambda: self.fail("expired receipt must not reexecute"), require_receipt=True,
            )
        with self.connect() as conn:
            terminal = conn.execute(
                "SELECT status,lease_until FROM qq_inbound_receipts WHERE platform_message_id='web:session-a:request-3'"
            ).fetchone()
        self.assertEqual("failed", terminal["status"])
        self.assertEqual("", terminal["lease_until"])

    def test_concurrent_same_web_request_has_one_operation_owner(self) -> None:
        from bridge_inbound_idempotency import InboundProcessingError, execute_once

        payload = {"source": "web-console", "user_id": "web-console", "message": "并发工作", "force": "auto"}
        started = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def operation():
            calls.append("owner")
            started.set()
            release.wait(timeout=3)
            return {"ok": True, "dispatch": "task", "task": {"id": "task-concurrent"}}

        owner = threading.Thread(
            target=lambda: execute_once(
                self.connect, "web:session-a:request-concurrent", "web:session-a", "web-console", payload,
                operation, require_receipt=True,
            ),
            daemon=True,
        )
        owner.start()
        self.assertTrue(started.wait(timeout=3))
        with self.assertRaisesRegex(InboundProcessingError, "web_dispatch_processing"):
            execute_once(
                self.connect, "web:session-a:request-concurrent", "web:session-a", "web-console", payload,
                lambda: self.fail("concurrent duplicate must not execute"), require_receipt=True,
            )
        release.set()
        owner.join(timeout=3)
        self.assertFalse(owner.is_alive())
        self.assertEqual(["owner"], calls)

    def test_qq_optional_receipt_keeps_existing_deferred_transaction_and_replay_contract(self) -> None:
        from bridge_inbound_idempotency import (
            InboundConflictError,
            InboundProcessingError,
            begin_receipt,
            execute_once,
        )
        from bridge_reliability_schema import RELIABILITY_FEATURE_FLAG

        with self.connect() as conn:
            conn.execute(
                "UPDATE assistant_feature_flags SET enabled=?,updated_at=? WHERE name=?",
                (1, "2026-08-11T00:00:00+00:00", RELIABILITY_FEATURE_FLAG),
            )
        statements: list[str] = []
        opened_connections: list[sqlite3.Connection] = []

        def traced_connect():
            conn = self.connect()
            conn.set_trace_callback(statements.append)
            opened_connections.append(conn)
            return conn

        payload = {"source": "qq", "user_id": "qq-user", "message": "保持 QQ 语义", "force": "auto"}
        calls: list[str] = []
        try:
            first = execute_once(
                traced_connect, "qq-regression-1", "qq-user", "qq:private:qq-user", payload,
                lambda: calls.append("owner") or {"ok": True, "dispatch": "chat"}, require_receipt=False,
            )
            replay = execute_once(
                traced_connect, "qq-regression-1", "qq-user", "qq:private:qq-user", payload,
                lambda: calls.append("duplicate") or {"ok": True}, require_receipt=False,
            )
            with self.assertRaisesRegex(InboundConflictError, "qq_message_id_payload_conflict"):
                execute_once(
                    traced_connect, "qq-regression-1", "qq-user", "qq:private:qq-user",
                    {**payload, "message": "不同内容"}, lambda: self.fail("QQ conflict must not execute"),
                    require_receipt=False,
                )
            begin_receipt(
                traced_connect, "qq-regression-processing", "qq-user", "qq:private:qq-user", payload,
                require_receipt=False,
            )
            with self.assertRaisesRegex(InboundProcessingError, "qq_message_processing"):
                execute_once(
                    traced_connect, "qq-regression-processing", "qq-user", "qq:private:qq-user", payload,
                    lambda: self.fail("QQ processing receipt must not execute"), require_receipt=False,
                )
        finally:
            for conn in opened_connections:
                conn.close()
        self.assertEqual(["owner"], calls)
        self.assertEqual(first, replay)
        self.assertFalse(any("BEGIN IMMEDIATE" in statement.upper() for statement in statements))

    def test_qq_reliability_disabled_keeps_existing_optional_no_receipt_behavior(self) -> None:
        from bridge_inbound_idempotency import execute_once

        calls: list[str] = []
        payload = {"source": "qq", "user_id": "qq-user", "message": "flag off", "force": "auto"}
        execute_once(
            self.connect, "qq-flag-off", "qq-user", "qq:private:qq-user", payload,
            lambda: calls.append("first") or {"ok": True}, require_receipt=False,
        )
        execute_once(
            self.connect, "qq-flag-off", "qq-user", "qq:private:qq-user", payload,
            lambda: calls.append("second") or {"ok": True}, require_receipt=False,
        )
        self.assertEqual(["first", "second"], calls)


class WebDispatchHttpIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.assistant_path = Path(self.directory.name) / "assistant.sqlite3"
        self.task_path = Path(self.directory.name) / "tasks.sqlite3"
        self.assistant_connect = _connect(self.assistant_path)
        self.task_connect = _connect(self.task_path)
        from bridge_reliability_schema import apply_task_message_reliability_v2

        with self.assistant_connect() as conn:
            conn.execute(
                "CREATE TABLE assistant_feature_flags(name TEXT PRIMARY KEY, enabled INTEGER NOT NULL, updated_at TEXT NOT NULL)"
            )
            apply_task_message_reliability_v2(conn)
        with self.task_connect() as conn:
            conn.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, message TEXT NOT NULL)")

        import codex_qq_bridge as bridge

        self.bridge = bridge
        self.saved = {
            name: getattr(bridge, name)
            for name in (
                "_assistant_db_connect", "_db_connect", "_assistant_dispatch",
                "_dispatch_qq_response_if_enabled", "with_qq_transport_metadata",
                "observe_private_participation", "bind_qq_response_decision", "_phase2_outbox",
            )
        }
        bridge._assistant_db_connect = self.assistant_connect
        bridge._db_connect = self.task_connect
        bridge.with_qq_transport_metadata = lambda payload, *_args, **_kwargs: dict(payload)
        bridge._dispatch_qq_response_if_enabled = lambda operation, *_args, **_kwargs: operation()
        bridge.observe_private_participation = lambda *_args, **_kwargs: {}
        bridge.bind_qq_response_decision = lambda *_args, **_kwargs: None
        bridge._phase2_outbox = lambda: None

        self.dispatch_calls = 0

        def dispatch(**kwargs):
            self.dispatch_calls += 1
            task_id = f"task-{self.dispatch_calls}"
            with self.task_connect() as conn:
                conn.execute("INSERT INTO tasks(id,message) VALUES(?,?)", (task_id, kwargs["message"]))
            return {"ok": True, "dispatch": "task", "reply": "created", "task": {"id": task_id}}

        bridge._assistant_dispatch = dispatch
        self.session_id = "v4-foundation-test-session"
        bridge.ADMIN_SESSIONS[self.session_id] = time.time() + 60
        self.server = bridge.ThreadingServer(("127.0.0.1", 0), bridge.BridgeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()
        self.bridge.ADMIN_SESSIONS.pop(self.session_id, None)
        for name, value in self.saved.items():
            setattr(self.bridge, name, value)
        self.directory.cleanup()

    def _post(
        self,
        request_id: str,
        message: str,
        actor_header: str = "client-controlled-value-must-not-scope-web-receipt",
        *,
        source: str = "web-console",
        include_request_id: bool = True,
    ) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        body = json.dumps({
            "user_id": "web-console", "source": source, "message": message,
            "trace_id": request_id, "force": "auto", "timeout": 30,
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-QQ-Actor-ID": actor_header,
            "Cookie": f"{self.bridge.ADMIN_SESSION_COOKIE}={self.session_id}",
        }
        if include_request_id:
            headers["X-QQ-Message-ID"] = request_id
        connection.request(
            "POST", "/assistant/dispatch", body=body,
            headers=headers,
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def test_http_dispatch_replays_one_task_and_rejects_payload_conflict(self) -> None:
        first_status, first = self._post("web-v4-http-1", "创建一项测试工作")
        replay_status, replay = self._post("web-v4-http-1", "创建一项测试工作", actor_header="different-client-actor")
        conflict_status, conflict = self._post("web-v4-http-1", "改成另一项工作")

        self.assertEqual(202, first_status)
        self.assertEqual(202, replay_status)
        self.assertEqual(first["task"]["id"], replay["task"]["id"])
        self.assertEqual(409, conflict_status)
        self.assertEqual("web_dispatch_request_id_payload_conflict", conflict["error"])
        self.assertEqual(1, self.dispatch_calls)
        with self.task_connect() as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])

    def test_legacy_workbench_submit_has_its_own_request_id_and_dispatches(self) -> None:
        workbench = (ROOT / "admin" / "views-workbench.js").read_text(encoding="utf-8")
        self.assertIn("web-workbench-", workbench)
        self.assertIn("let homeDispatchPending = false", workbench)
        self.assertIn("'X-QQ-Message-ID': request.id", workbench)
        status, result = self._post("web-workbench-regression-1", "Legacy workbench submit")
        self.assertEqual(202, status)
        self.assertTrue(result["ok"])
        self.assertEqual(1, self.dispatch_calls)

    def test_admin_principal_cannot_opt_out_of_receipt_by_changing_payload_source(self) -> None:
        first_status, first = self._post(
            "admin-spoof-1", "source spoof must remain protected", source="anything-else",
        )
        replay_status, replay = self._post(
            "admin-spoof-1", "source spoof must remain protected", source="anything-else",
        )
        missing_status, missing = self._post(
            "", "missing IDs must not execute", source="anything-else", include_request_id=False,
        )
        self.assertEqual(202, first_status)
        self.assertEqual(202, replay_status)
        self.assertEqual(first["task"]["id"], replay["task"]["id"])
        self.assertEqual(400, missing_status)
        self.assertEqual("web_dispatch_request_id_required", missing["error"])
        self.assertEqual(1, self.dispatch_calls)

    def test_internal_value_error_is_sanitized_not_reclassified_as_client_input(self) -> None:
        self.bridge._assistant_dispatch = lambda **_kwargs: (_ for _ in ()).throw(
            ValueError("internal-sensitive-detail-marker")
        )
        status, result = self._post("internal-value-error-1", "inject internal failure")
        self.assertEqual(500, status)
        self.assertEqual("assistant_dispatch_failed", result["error"])
        self.assertNotIn("internal-sensitive-detail-marker", json.dumps(result, ensure_ascii=False))

    def test_admin_token_receipt_scope_is_stable_and_does_not_persist_secret_material(self) -> None:
        from bridge_auth import PrincipalKind

        class Handler:
            headers = {"X-QQ-Message-ID": "admin-token-request-1"}

        first = self.bridge._web_dispatch_receipt_context(Handler(), PrincipalKind.ADMIN_TOKEN)
        second = self.bridge._web_dispatch_receipt_context(Handler(), PrincipalKind.ADMIN_TOKEN)
        self.assertEqual(first, second)
        self.assertNotIn("admin-token", first["platform_message_id"])


class V4PackageProvenanceTests(unittest.TestCase):
    def test_current_builder_records_git_and_content_provenance_separately(self) -> None:
        import importlib.util

        builder_path = ROOT / "tools" / "build_v4_1_reconstruction_patch.py"
        spec = importlib.util.spec_from_file_location("v4_foundation_builder", builder_path)
        assert spec and spec.loader
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "foundation.zip"
            result = builder.build(output)
            self.assertTrue(result["content_manifest_sha256"])
            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read(
                    f"{builder.PACKAGE_PREFIX}/PATCH_MANIFEST.json"
                ))
        self.assertTrue(result["content_manifest_sha256"])
        self.assertEqual(result["content_manifest_sha256"], manifest["content_manifest_sha256"])
        if not result["git_checkout_available"]:
            self.skipTest("Git provenance is verified only inside a Git checkout; archive content remains verifiable.")
        self.assertTrue(result["git_base_revision"])
        self.assertEqual(result["git_base_revision"], manifest["git_base_revision"])
        self.assertEqual(result["working_tree_clean"], manifest["working_tree_clean"])
        self.assertNotIn("base_revision", manifest)

    def test_builder_remains_reviewable_without_git_metadata(self) -> None:
        import importlib.util

        builder_path = ROOT / "tools" / "build_v4_1_reconstruction_patch.py"
        spec = importlib.util.spec_from_file_location("v4_foundation_builder_archive", builder_path)
        assert spec and spec.loader
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        original_run = builder.subprocess.run

        def unavailable_git(*_args, **_kwargs):
            raise subprocess.CalledProcessError(128, ["git", "rev-parse", "HEAD"])

        builder.subprocess.run = unavailable_git
        try:
            provenance = builder.source_provenance()
        finally:
            builder.subprocess.run = original_run
        self.assertFalse(provenance["git_checkout_available"])
        self.assertIsNone(provenance["git_base_revision"])


if __name__ == "__main__":
    unittest.main()
