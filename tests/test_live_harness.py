"""The real-service harness rejects unrelated resources and unsafe artifact paths."""

import ast
import asyncio
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


spec = importlib.util.spec_from_file_location("live_harness", Path(__file__).parents[1] / "tools/run-live-e2e.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


class LiveHarnessTests(unittest.TestCase):
    def test_actual_extraction_can_use_json_schema_or_json_object(self):
        self.assertTrue(harness.extraction_response({"response_format": {
            "type": "json_schema", "json_schema": {"schema": {"properties": {"facts": {}}}},
        }}, b"{}"))
        response = json.dumps({"choices": [{"message": {"content": '{"facts": []}'}}]}).encode()
        self.assertTrue(harness.extraction_response({"response_format": {"type": "json_object"}}, response))

    def test_fault_gate_does_not_hold_chat_consolidation_or_invalid_responses(self):
        for content in ('{"creates": [], "updates": [], "deletes": []}', "invalid", "[]"):
            response = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.assertFalse(harness.extraction_response({"response_format": {"type": "json_object"}}, response))
        self.assertFalse(harness.extraction_response({}, b"{}"))
        self.assertFalse(harness.extraction_response({"response_format": {"type": "json_object"}}, b"{}"))

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
