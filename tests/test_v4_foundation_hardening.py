from __future__ import annotations

import http.client
import json
import sqlite3
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

    def _post(self, request_id: str, message: str, actor_header: str = "client-controlled-value-must-not-scope-web-receipt") -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        body = json.dumps({
            "user_id": "web-console", "source": "web-console", "message": message,
            "trace_id": request_id, "force": "auto", "timeout": 30,
        }).encode("utf-8")
        connection.request(
            "POST", "/assistant/dispatch", body=body,
            headers={
                "Content-Type": "application/json",
                "X-QQ-Message-ID": request_id,
                "X-QQ-Actor-ID": actor_header,
                "Cookie": f"{self.bridge.ADMIN_SESSION_COOKIE}={self.session_id}",
            },
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
            self.assertTrue(result["git_base_revision"])
            self.assertTrue(result["content_manifest_sha256"])
            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read(
                    "NekoAgent-V4.1-Foundation-Slices-2026-08-11-r1/PATCH_MANIFEST.json"
                ))
        self.assertEqual(result["git_base_revision"], manifest["git_base_revision"])
        self.assertEqual(result["working_tree_clean"], manifest["working_tree_clean"])
        self.assertEqual(result["content_manifest_sha256"], manifest["content_manifest_sha256"])
        self.assertNotIn("base_revision", manifest)


if __name__ == "__main__":
    unittest.main()
