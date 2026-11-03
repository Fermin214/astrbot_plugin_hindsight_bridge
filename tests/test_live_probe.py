"""The live-test harness keeps credentials out of output and blocks redirects."""

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError


spec = importlib.util.spec_from_file_location("qwen_probe", Path(__file__).parent / "integration/qwen_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class LiveProbeTests(unittest.TestCase):
    def test_key_parser_accepts_only_named_test_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            for content in ("key=fixture-secret", 'export DASHSCOPE_API_KEY="fixture-secret"',
                            "# ignored\nQWEN_API_KEY='fixture-secret'\n"):
                path.write_text(content)
                self.assertEqual(probe.read_key(path), "fixture-secret")
            path.write_text("UNRELATED_SECRET=fixture-secret\nkey=\n")
            with self.assertRaisesRegex(ValueError, "No test API key found"):
                probe.read_key(path)

    def test_redirects_do_not_forward_credentials(self):
        self.assertIsNone(probe.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.invalid"))

    def test_upstream_error_body_and_message_are_not_reported(self):
        error = HTTPError("https://test.invalid", 401, "fixture-secret", {},
                          io.BytesIO(b'{"code":"InvalidApiKey", "message":"fixture-secret"}'))
        opener = Mock()
        opener.open.side_effect = error
        body, report = probe.post(opener, "fixture-secret", "https://test.invalid", {})
        self.assertIsNone(body)
        self.assertEqual(report, {"http_status": 401, "code": "InvalidApiKey"})
        self.assertNotIn("fixture-secret", json.dumps(report))

    def test_network_errors_do_not_report_exception_messages(self):
        opener = Mock()
        opener.open.side_effect = URLError("fixture-secret")
        body, report = probe.post(opener, "fixture-secret", "https://test.invalid", {})
        self.assertIsNone(body)
        self.assertEqual(report, {"error": "network_or_timeout"})

    def test_live_probe_uses_small_fixed_inputs_and_clean_report(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / ".env"
            output = Path(directory) / "report.json"
            secret.write_text("key=fixture-secret")
            replies = [({"output": {"embeddings": [{"embedding": [0.2, 0.3]}] * 3}}, {"http_status": 200}),
                       ({"output": {"results": [{"index": 1, "relevance_score": 0.9},
                                                 {"index": 0, "relevance_score": 0.1}]}}, {"http_status": 200})]
            with patch("sys.argv", ["probe", "--secret-file", str(secret), "--output", str(output)]), \
                    patch.object(probe, "post", side_effect=replies) as post, redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(probe.main(), 0)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(len(post.call_args_list[0].args[3]["input"]["texts"]), 3)
            self.assertEqual(len(post.call_args_list[1].args[3]["input"]["documents"]), 2)
            self.assertNotIn("fixture-secret", output.read_text() + stdout.getvalue())
