"""The real-service harness rejects unrelated resources and unsafe artifact paths."""

import ast
import asyncio
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.request import Request


spec = importlib.util.spec_from_file_location("live_harness", Path(__file__).parents[1] / "tools/run-live-e2e.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


class LiveHarnessTests(unittest.TestCase):
    def test_gateway_relays_first_stream_event_before_upstream_finishes(self):
        release = threading.Event()
        finished = threading.Event()
        first = b'data: {"choices":[{"delta":{"content":"alpha"}}]}\n\n'
        last = b"data: [DONE]\n\n"

        class Upstream(harness.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.end_headers()
                self.wfile.write(first)
                self.wfile.flush()
                release.wait(4)
                self.wfile.write(last)
                self.wfile.flush()
                finished.set()

        with tempfile.TemporaryDirectory() as directory:
            upstream = harness.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            state = SimpleNamespace(
                output=Path(directory), settings={"chat_token": "synthetic-chat-token"},
                magpie="http://127.0.0.1:" + str(upstream.server_port) + "/v1",
            )
            gateway = harness.Gateway(state)
            body = {"stream": True, "messages": [{"role": "user", "content": "synthetic"}]}
            try:
                with harness.serving(upstream), harness.serving(gateway):
                    request = Request(
                        "http://127.0.0.1:" + str(gateway.server_port) + "/v1/chat/completions",
                        data=json.dumps(body).encode(),
                        headers={"Content-Type": "application/json",
                                 "Authorization": "Bearer synthetic-chat-token"},
                    )
                    with gateway.opener.open(request, timeout=2) as response:
                        self.assertEqual(response.headers.get_content_type(), "text/event-stream")
                        received = response.readline() + response.readline()
                        self.assertEqual(received, first)
                        self.assertFalse(finished.is_set())
                        release.set()
                        self.assertEqual(response.read(), last)
            finally:
                release.set()
            trace = json.loads((Path(directory) / "chat-stream-trace.jsonl").read_text())
            self.assertEqual(trace["request"], body)
            self.assertEqual(trace["response_sse"].encode(), first + last)
            self.assertFalse((Path(directory) / "chat-trace.jsonl").exists())

    def test_actual_extraction_can_use_json_schema_or_json_object(self):
        item = {"properties": {"what": {"type": "string"}, "fact_type": {"type": "string"}}}
        self.assertTrue(harness.extraction_response({"response_format": {
            "type": "json_schema", "json_schema": {"schema": {"properties": {"facts": {"items": item}}}},
        }}, b"{}"))
        self.assertTrue(harness.extraction_response({"response_format": {
            "type": "json_schema", "json_schema": {"schema": {
                "$defs": {"ExtractedFact": item}, "properties": {"facts": {"items": {"$ref": "#/$defs/ExtractedFact"}}},
            }},
        }}, b"{}"))
        response = json.dumps({"choices": [{"message": {"content": '{"facts": [{"what": "fixture", "fact_type": "world"}]}'}}]}).encode()
        self.assertTrue(harness.extraction_response({"response_format": {"type": "json_object"}}, response))

    def test_fault_gate_does_not_hold_chat_consolidation_or_invalid_responses(self):
        for content in ('{"creates": [], "updates": [], "deletes": []}', "invalid", "[]", '{"facts": []}',
                        '{"facts": [{"fact_id": "F1", "about": "fixture", "kind": "lasting", "adds": true}]}'):
            response = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.assertFalse(harness.extraction_response({"response_format": {"type": "json_object"}}, response))
        self.assertFalse(harness.extraction_response({}, b"{}"))
        self.assertFalse(harness.extraction_response({"response_format": {"type": "json_object"}}, b"{}"))

    def test_1_2_consolidation_schema_does_not_trigger_extraction_fault(self):
        for item in ({}, {"$ref": "#/$defs/Fact"}, None):
            schema = {"properties": {"facts": {"items": item}}, "$defs": {
                "Fact": {"properties": {"fact_id": {}, "kind": {}, "adds": {}}},
            }}
            self.assertFalse(harness.extraction_response({"response_format": {
                "type": "json_schema", "json_schema": {"schema": schema},
            }}, b"{}"))

    def test_onebot_message_identity_remains_unique_after_socket_reconnect(self):
        path = Path(__file__).parent / "integration/live_e2e.py"
        tree = ast.parse(path.read_text())
        peer = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OneBot")
        namespace = {"asyncio": asyncio, "time": time, "BOT": 990001}
        exec(compile(ast.Module(body=[peer], type_ignores=[]), str(path), "exec"), namespace)
        async def send():
            before, after = namespace["OneBot"](None, {}), namespace["OneBot"](None, {})
            before.socket = after.socket = AsyncMock()
            first = await before.send(123001, "before restart")
            second = await after.send(123001, "after restart")
            self.assertGreater(second, first)
        asyncio.run(send())

    def test_resource_owner_mismatch_blocks_service_control(self):
        state = object.__new__(harness.Environment)
        state.owner = "owned-test"
        state.docker = Mock()
        with patch.object(harness.subprocess, "check_output", return_value="unrelated-service\n"):
            with self.assertRaisesRegex(RuntimeError, "does not belong"):
                state.control("restart_hindsight")
        state.docker.assert_not_called()

    def test_containers_are_tracked_before_launch_and_have_owned_labels(self):
        state = object.__new__(harness.Environment)
        state.owner, state.containers = "owned-test", []
        def failing_launch(*args, **options):
            self.assertEqual(state.containers, ["owned-test-astrbot"])
            self.assertIn("hindsight-bridge.test=owned-test", args)
            raise RuntimeError("startup failed")
        state.docker = Mock(side_effect=failing_launch)
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            state.launch("astrbot", "official-test-image")

    def test_private_files_have_0600_permissions_even_when_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("old")
            path.chmod(0o644)
            harness.private_json(path, {"synthetic": "value"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_saved_cleanup_stops_registered_process_and_ignores_a_reused_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            state = object.__new__(harness.Environment)
            state.output, state.owner = Path(directory), "hb-live-12345678"
            state.resources = {"owner": state.owner, "projects": [], "processes": []}
            state.save_resources()
            process = state.start_process("model-proxy", [sys.executable, "-c", "import time; time.sleep(30)"])
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
            try:
                state.resources["processes"].append({"role": "stale-proxy", "pid": unrelated.pid, "start": "old"})
                state.save_resources()
                restored = harness.Environment.load_resources(state.output)
                attached = restored.start_process("tunnel", [sys.executable, "-c", "import time; time.sleep(30)"])
                state.save_resources()
                with patch.object(state, "remove_resources"):
                    state.cleanup()
                self.assertEqual(process.wait(timeout=3), -harness.signal.SIGTERM)
                self.assertEqual(attached.wait(timeout=3), -harness.signal.SIGTERM)
                self.assertIsNone(unrelated.poll())
                self.assertTrue((state.output / "model-proxy.log").exists())
                self.assertEqual(stat.S_IMODE((state.output / "acceptance-resources.json").stat().st_mode), 0o600)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if "attached" in locals() and attached.poll() is None:
                    attached.kill()
                    attached.wait()
                unrelated.terminate()
                unrelated.wait()

    def test_cleanup_stops_a_child_after_its_launcher_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            state = object.__new__(harness.Environment)
            state.output, state.owner = Path(directory), "hb-live-12345678"
            state.resources = {"owner": state.owner, "projects": [], "processes": []}
            state.save_resources()
            marker = state.output / "child.pid"
            child = ("import os,signal,time;from pathlib import Path;"
                     "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                     f"Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(30)")
            launcher = f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{child!r}])"
            process = state.start_process("proxy-launcher", [sys.executable, "-c", launcher])
            try:
                process.wait(timeout=3)
                deadline = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                pid = int(marker.read_text())
                self.assertIsNotNone(harness.Environment.process_start(pid))
                with patch.object(state, "remove_resources"):
                    state.cleanup()
                self.assertIsNone(harness.Environment.process_start(pid))
            finally:
                try:
                    harness.os.killpg(process.pid, harness.signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_cleanup_closes_registration_before_removing_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            state = object.__new__(harness.Environment)
            state.output, state.owner = Path(directory), "hb-live-12345678"
            state.resources = {"owner": state.owner, "projects": [], "processes": []}
            state.save_resources()
            started, release = threading.Event(), threading.Event()
            failures = []
            def removing(*args, **kwargs):
                started.set()
                release.wait(3)
            def clean():
                try:
                    state.cleanup()
                except Exception as error:
                    failures.append(error)
            with patch.object(state, "remove_resources", side_effect=removing):
                worker = threading.Thread(target=clean)
                worker.start()
                try:
                    self.assertTrue(started.wait(3))
                    attached = harness.Environment.load_resources(state.output)
                    with patch.object(harness.subprocess, "Popen") as launch:
                        with self.assertRaisesRegex(RuntimeError, "closed"):
                            attached.start_process("late-proxy", [sys.executable, "-c", "pass"])
                        launch.assert_not_called()
                finally:
                    release.set()
                    worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])

    def test_cleanup_attempts_other_owned_resources_after_one_project_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            state = object.__new__(harness.Environment)
            state.output, state.owner = Path(directory), "hb-live-12345678"
            state.resources = {"owner": state.owner, "processes": [], "projects": ["astrbot-memory-123456789abc"]}
            state.save_resources()
            state.remove_resources = Mock(side_effect=[RuntimeError("project cleanup failed"), None])
            with self.assertRaisesRegex(RuntimeError, "project cleanup failed"):
                state.cleanup()
            self.assertEqual(state.remove_resources.call_count, 2)
            self.assertEqual(state.remove_resources.call_args.args[0], "hindsight-bridge.test=" + state.owner)

    def test_existing_and_in_repository_output_paths_are_rejected(self):
        for path in (harness.ROOT / ".local/live-output", harness.ROOT):
            with self.assertRaisesRegex(ValueError, "fresh directory outside"):
                harness.Environment(SimpleNamespace(output=path))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "existing.txt").write_text("preserve")
            with self.assertRaisesRegex(ValueError, "fresh directory outside"):
                harness.Environment(SimpleNamespace(output=path))
            self.assertEqual((path / "existing.txt").read_text(), "preserve")

    def test_onebot_peer_preserves_plain_forwarded_and_file_message_segments(self):
        path = Path(__file__).parent / "integration/live_e2e.py"
        tree = ast.parse(path.read_text())
        helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "message_segments")
        namespace = {}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(path), "exec"), namespace)
        segments = namespace["message_segments"]
        file = {"type": "file", "data": {"file": "file:///delivery/export.json"}}
        self.assertEqual(segments("reply"), [{"type": "text", "data": {"text": "reply"}}])
        self.assertEqual(segments([{"type": "node", "data": {"content": ["reply", file]}}]),
                         [{"type": "text", "data": {"text": "reply"}}, file])

    def test_onebot_peer_handles_nested_forward_nodes_without_fabricating_text(self):
        path = Path(__file__).parent / "integration/live_e2e.py"
        tree = ast.parse(path.read_text())
        helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "message_segments")
        namespace = {}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(path), "exec"), namespace)
        self.assertEqual(namespace["message_segments"]({"type": "node", "data": {"content": [
            {"type": "node", "data": {"content": "nested"}}, None,
        ]}}), [{"type": "text", "data": {"text": "nested"}}])
