"""Probe privacy and fresh-root isolation need direct negative checks."""

import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load("platform_probe", "tests/integration/platform_probe/probe.py")
preparation = load("prepare_platform", "tools/prepare-platform-acceptance.py")


class PlatformProbeTests(unittest.TestCase):
    def test_samples_expire_and_do_not_accept_events_queued_before_arm(self):
        samples = probe.Samples()
        pair = ("instance", "user")
        samples.arm(pair, "text", 100, {"available": True}, received_at=99.5)
        self.assertIsNone(samples.take(("instance", "another-user"), 101, 100))
        self.assertIsNone(samples.take(pair, 101, 99))
        self.assertEqual(samples.take(pair, 101, 100), ("text", {"available": True}))
        samples.arm(pair, "voice", 100, {}, received_at=99.5)
        self.assertIsNone(samples.take(pair, 221, 220))
        samples.arm(pair, "text", 100, {}, received_at=99.5)
        self.assertEqual(samples.take(pair, 101, 100), ("text", {}))
        self.assertIsNone(samples.take(pair, 101, 100))

    def test_only_whitelisted_properties_reach_report(self):
        raw = {
            "message_id": 9007199254740993123, "from_user_id": "private-user",
            "to_user_id": "private-bot", "create_time_ms": 1700000000001,
            "message_type": 1, "message_state": 2,
            "context_token": "secret-context", "credential": {"token": "secret-token"},
            "item_list": [{"type": 1, "text_item": {"text": "private text", "secret": "private"}}],
        }
        event = SimpleNamespace(
            get_platform_name=lambda: "weixin_oc", get_platform_id=lambda: "private-instance",
            get_sender_id=lambda: "private-user", get_group_id=lambda: "",
            get_message_str=lambda: "private text", get_messages=lambda: [],
            is_private_chat=lambda: True, created_at=1700000001,
            message_obj=SimpleNamespace(
                raw_message=raw, message_id=str(raw["message_id"]), timestamp=1700000000,
            ),
        )
        serialized = json.dumps(probe.project(event, None, b"test-key"))
        for secret in (
            "private-user", "private-bot", "private-instance", "private text",
            "secret-context", "secret-token", "credential", "9007199254740993123",
        ):
            self.assertNotIn(secret, serialized)
        self.assertNotEqual(
            probe.fingerprint("private-user", b"first"),
            probe.fingerprint("private-user", b"second"),
        )
        raw["update_time_ms"] = raw["create_time_ms"] + 1
        raw["delete_time_ms"] = raw["create_time_ms"] + 2
        raw["item_list"][0]["is_completed"] = False
        result = probe.project(event, None, b"test-key")
        self.assertTrue(result["has_later_update"])
        self.assertTrue(result["has_deletion"])
        self.assertTrue(result["items"][0]["explicitly_incomplete"])
        self.assertEqual(result["unknown_field_count"], 1)

    def test_preparation_cannot_overwrite_existing_root_and_binds_loopback(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "new-host"
            manifest = preparation.prepare(destination, "weixin_oc", "4.29.4", 6188)
            self.assertEqual(manifest["platform"], "weixin_oc")
            compose = json.loads((destination / "compose.yaml").read_text())
            self.assertEqual(compose["services"]["astrbot"]["ports"], ["127.0.0.1:6188:6185"])
            self.assertEqual((destination / ".env").stat().st_mode & 0o777, 0o600)
            self.assertEqual((destination / "data/cmd_config.json").stat().st_mode & 0o777, 0o600)
            probe_config = json.loads((destination / "data/plugins" / preparation.PROBE / "_conf_schema.json").read_text())
            self.assertFalse(probe_config["enabled"]["default"])
            before = (destination / ".env").read_bytes()
            with self.assertRaisesRegex(ValueError, "new directory"):
                preparation.prepare(destination, "telegram", "4.29.4", 6189)
            self.assertEqual((destination / ".env").read_bytes(), before)
