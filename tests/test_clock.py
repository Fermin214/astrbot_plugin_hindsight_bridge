"""Clock fallback, historical DST, and immutable requests across host changes."""

import ast
import asyncio
import copy
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from plugins.astrbot_plugin_hindsight_bridge.core.clock import host_timezone
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming, sha256
from plugins.astrbot_plugin_hindsight_bridge.core.materials import Message
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox, QueueError
from plugins.astrbot_plugin_hindsight_bridge.core.permissions import pairs


def lifecycle_methods(namespace):
    """Load real lifecycle methods without AstrBot hooks or a web server."""
    source = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    bridge = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "HindsightBridge"
    )
    methods = [
        node for node in bridge.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"initialize", "refresh_permissions"}
    ]
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


class ClockResolution(unittest.TestCase):
    def test_empty_invalid_and_non_string_host_zones_use_system_time(self):
        for config in (
            None, {}, {"timezone": ""}, {"timezone": None},
            {"timezone": "No/Such_Zone"}, {"timezone": "../etc/localtime"},
            {"timezone": "/etc/localtime"}, {"timezone": 8},
        ):
            with self.subTest(config=config):
                context = SimpleNamespace(get_config=Mock(return_value=config))
                self.assertIsNone(host_timezone(context))
                context.get_config.assert_called_once_with()
        context = SimpleNamespace(get_config=lambda: {
            "timezone": "Asia/Shanghai",
            "provider_settings": {"datetime_system_prompt": False},
        })
        self.assertEqual(host_timezone(context), "Asia/Shanghai")

    @unittest.skipUnless(hasattr(time, "tzset"), "requires POSIX system timezone rules")
    def test_system_timezone_uses_message_date_across_dst_and_repeated_hour(self):
        # POSIX rules make this independent of the container's configured timezone.
        try:
            with patch.dict(os.environ, {"TZ": "EST5EDT,M3.2.0,M11.1.0"}):
                time.tzset()
                for utc, expected in (
                    ("2026-01-15T12:00:00", "2026-01-15T07:00:00-05:00"),
                    ("2026-07-15T12:00:00", "2026-07-15T08:00:00-04:00"),
                    ("2026-11-01T05:30:00", "2026-11-01T01:30:00-04:00"),
                    ("2026-11-01T06:30:00", "2026-11-01T01:30:00-05:00"),
                ):
                    with self.subTest(utc=utc):
                        stamp = int(datetime.fromisoformat(utc).replace(tzinfo=UTC).timestamp())
                        message = Message("event", "text", stamp)
                        self.assertEqual(message.iso_time(None), expected)
                        self.assertEqual(message.iso_time("America/New_York"), expected)
        finally:
            time.tzset()


class HostClockLifecycle(unittest.IsolatedAsyncioTestCase):
    async def initialize_bridge(self, root, host):
        namespace = lifecycle_methods({
            "asyncio": asyncio, "Path": Path, "sha256": sha256, "pairs": pairs,
            "host_timezone": host_timezone, "Outbox": Outbox, "QueueError": QueueError,
            "encoding": Mock(), "logger": Mock(),
            "HindsightClient": Mock(side_effect=lambda url, key, **kw: SimpleNamespace(url=url)),
            "StarTools": SimpleNamespace(get_data_dir=lambda name: root / "runtime"),
            "Worker": Mock(return_value=SimpleNamespace(run=AsyncMock())),
        })
        plugin = SimpleNamespace(
            config={
                "api_url": "http://memory.test", "api_key": "key",
                "service_id": "clock-test",
                "allowed_accounts": [{"platform_instance_id": "onebot", "user_id": "alice"}],
            },
            context=SimpleNamespace(get_config=Mock(return_value=host)),
            outbox=None, permissions=None, permission_revision=0,
            control_directory=lambda: root / "control", warning=Mock(),
            _close_resources=AsyncMock(),
        )
        for name in ("initialize", "refresh_permissions"):
            setattr(plugin, name, MethodType(namespace[name], plugin))
        await plugin.initialize()
        self.assertEqual(plugin.status, "已配置")
        await plugin.task
        plugin.outbox.recovering = False
        self.addCleanup(plugin.outbox.close)
        return plugin

    async def test_initialize_without_browser_or_valid_host_timezone(self):
        with tempfile.TemporaryDirectory() as directory:
            for index, host in enumerate(({}, {"timezone": "No/Such_Zone"})):
                with self.subTest(host=host):
                    plugin = await self.initialize_bridge(Path(directory) / str(index), host)
                    self.assertIsNone(plugin.outbox.zone)
                    self.assertNotIn("timezone", plugin.config)
                    plugin.outbox.close()

    async def test_host_changes_switch_new_materials_without_rewriting_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            host = {
                "timezone": "Asia/Shanghai",
                "provider_settings": {"datetime_system_prompt": False},
            }
            original_host = copy.deepcopy(host)
            plugin = await self.initialize_bridge(Path(directory), host)
            queue = plugin.outbox
            self.assertEqual(queue.zone, "Asia/Shanghai")
            source = Incoming("aiocqhttp", "onebot", "bot", "alice", "first", int(time.time()), "hello")
            first = queue.capture(source, "conversation")
            before = dict(queue.get(first.task_id))
            self.assertEqual(
                queue.verify(before)["items"][0]["metadata"]["timezone"], "Asia/Shanghai"
            )
            revision = plugin.permission_revision
            for index, (value, expected) in enumerate((
                ("America/New_York", "America/New_York"),
                ("", "system"),
                ("No/Such_Zone", "system"),
                ("UTC", "UTC"),
            )):
                plugin.context.get_config.return_value = {"timezone": value}
                plugin.refresh_permissions()
                self.assertEqual(queue.zone, None if expected == "system" else expected)
                captured = queue.capture(replace(source, message_id=str(index)), "conversation")
                row = queue.get(captured.task_id)
                item = queue.verify(row)["items"][0]
                self.assertEqual(item["metadata"]["timezone"], expected)
                stamp = Message("event", "hello", source.timestamp).iso_time(queue.zone)
                self.assertEqual(item["timestamp"], stamp)
                self.assertEqual(item["metadata"]["occurred_at"], stamp)
                self.assertIn(stamp, item["content"])
                assistant = queue.capture_assistant(captured.task_id, "reply")
                assistant_item = queue.verify(queue.get(assistant.task_id))["items"][0]
                self.assertEqual(assistant_item["metadata"]["timezone"], expected)
                self.assertEqual(dict(queue.get(first.task_id)), before)
                self.assertEqual(queue.verify(before), json.loads(before["payload"]))
            self.assertFalse(queue.capture(source, "conversation").created)
            self.assertEqual(dict(queue.get(first.task_id)), before)
            self.assertEqual(plugin.permission_revision, revision)
            self.assertEqual(host, original_host)
            queue.close()
