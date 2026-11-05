"""Run native bridge hook and command methods with controlled host I/O."""

import asyncio
import itertools
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_runtime_diagnostics import bridge_class, Configuration, Plain
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming
from plugins.astrbot_plugin_hindsight_bridge.core.client import HindsightClient
from plugins.astrbot_plugin_hindsight_bridge.core.maintenance import Maintenance
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox
from plugins.astrbot_plugin_hindsight_bridge.core.panel import MemoryPanel


class SourceHookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ticks = itertools.count(100250)
        self.clock = patch.object(time, "time", side_effect=lambda: next(ticks) / 1000)
        self.clock.start()
        self.bot = SimpleNamespace(get_login_info=AsyncMock(return_value={"user_id": 1001}))
        self.adapter = SimpleNamespace(get_client=lambda: self.bot)
        self.context = SimpleNamespace(
            get_config=lambda *args: {
                "platform": [{"id": "qq", "type": "aiocqhttp"}], "wake_prefix": ["/"],
            },
            get_platform_inst=lambda identifier: self.adapter,
        )
        self.config = Configuration(allowed_accounts=[{"platform_instance_id": "qq", "user_id": "2001"}])
        self.plugin = bridge_class(self.root, Mock())(self.context, self.config)
        self.plugin.outbox = Outbox(
            self.root / "runtime", "service", "http://memory.test",
            credential_fingerprint="test", control_directory=self.root / "control",
        )
        self.plugin.worker = SimpleNamespace(
            wake=asyncio.Event(), maintenance=SimpleNamespace(check=AsyncMock(return_value=True)),
        )
        self.plugin.refresh_permissions()
        self.incoming = Incoming("aiocqhttp", "qq", "1001", "2001", "message", 101, "hello")
        self.plugin.outbox.activate(
            self.incoming, self.plugin.outbox.access.authorization("qq", "2001")["revision"],
        )
        await self.plugin.prepare_sources()

    async def asyncTearDown(self):
        self.plugin.outbox.close()
        self.clock.stop()
        self.tmp.cleanup()

    def event(self, text="hello", mid="message", timestamp=101, bot=1001):
        raw = {
            "post_type": "message", "message_type": "private", "self_id": bot,
            "user_id": 2001, "message_id": mid, "time": timestamp,
            "message": [{"type": "text", "data": {"text": text}}],
        }
        return SimpleNamespace(
            bot=self.bot, get_platform_name=lambda: "aiocqhttp",
            get_platform_id=lambda: "qq", get_self_id=lambda: str(bot),
            get_sender_id=lambda: "2001", is_private_chat=lambda: True,
            get_group_id=lambda: "", get_message_str=lambda: text,
            get_messages=lambda: [Plain(text)], unified_msg_origin="qq:FriendMessage:2001",
            message_obj=SimpleNamespace(raw_message=raw, message_id=mid),
            created_at=time.time(),
            plain_result=lambda text: text,
        )

    async def test_command_uses_same_original_identity_and_time_as_capture(self):
        event = self.event("/hindsight pause")
        self.assertIsNone(self.plugin.snapshot(event))
        scope = self.plugin.command_scope(event)
        self.assertEqual(scope["scope_id"], self.plugin.outbox.scope(self.incoming)["scope_id"])
        event.message_obj.raw_message["time"] = None
        self.assertIsNone(self.plugin.command_scope(event))

    async def test_native_forget_waits_for_upgrade_including_an_earlier_confirmation(self):
        panel = self.plugin.panel
        panel.directory = lambda: self.root / "setup"
        self.config.update(api_url="http://memory.test", api_key="valid-key")
        owned = {"id": "012345abcdef", "api_url": self.config["api_url"], "api_key": self.config["api_key"]}
        scope = dict(self.plugin.outbox.scope(self.incoming))
        awaitable = self.plugin.memory_forget(self.event("/hindsight forget", mid="forget-prepare"))
        replies = [text async for text in awaitable]
        self.assertIn("120 秒", str(replies))
        token = self.plugin.confirmations[scope["owner_id"]]["token"]
        panel.store_state({**owned, "upgrade_check": {
            "connection_id": self.plugin.outbox.connection_id, "verified": False,
        }})
        for index, confirmation in enumerate(("", token)):
            replies = [text async for text in self.plugin.memory_forget(
                self.event(
                    "/hindsight forget" + (" " + confirmation if confirmation else ""),
                    mid="forget-blocked-" + str(index), timestamp=102 + index,
                ), confirmation,
            )]
            self.assertIn("服务升级期间", str(replies))
            self.assertEqual(self.plugin.outbox.scope(self.incoming)["bank"], scope["bank"])
        panel.store_state({**owned, "upgrade_check": {
            "connection_id": self.plugin.outbox.connection_id, "verified": True,
        }})
        replies = [text async for text in self.plugin.memory_forget(
            self.event("/hindsight forget " + token, mid="forget-confirm", timestamp=104), token,
        )]
        self.assertIn("正在清空", str(replies))
        self.assertNotEqual(self.plugin.outbox.scope(self.incoming)["bank"], scope["bank"])

    async def test_native_commands_recover_with_explicit_bank_resume_after_retained_upgrade(self):
        panel = self.plugin.panel
        panel.directory = lambda: self.root / "setup"
        self.config.update(api_url="http://memory.test", api_key="replacement-key")
        client = HindsightClient(self.config["api_url"], self.config["api_key"])
        self.addAsyncCleanup(client.close)
        client.ensure_auth = AsyncMock()
        client.bank_exists = AsyncMock(return_value=True)
        client.bank_list_contains = AsyncMock(return_value=False)
        client.list_operations = AsyncMock(return_value=[])
        self.plugin.client = client
        queue = self.plugin.outbox
        self.plugin.worker.maintenance = Maintenance(queue, client)
        scope = dict(queue.scope(self.incoming))
        self.plugin.worker.maintenance.begin(scope["scope_id"], management=queue.management(scope["scope_id"]))
        panel.store_state({
            "id": "012345abcdef", "api_url": self.config["api_url"], "api_key": "previous-key",
            "upgrade_check": {"connection_id": queue.connection_id, "verified": False},
            "upgrade_pauses": {"connection_id": queue.connection_id, "banks": []},
        })
        self.assertFalse(panel.managed_connection())
        self.assertIn("已暂停", await self.plugin.mode(self.event("/hindsight on", mid="on-paused"), "active"))
        self.assertTrue(queue.summary(scope["scope_id"])["maintenance"])
        panel.body = AsyncMock(return_value={"action": "maintenance_resume"})
        with patch.dict(sys.modules, {"astrbot.api.web": SimpleNamespace(
            request=SimpleNamespace(method="POST", username="admin"),
        )}):
            await panel.action(scope["scope_id"])
        self.assertIsNone(queue.summary(scope["scope_id"])["maintenance"])
        self.assertIn("已开启", await self.plugin.mode(
            self.event("/hindsight on", mid="on-resumed", timestamp=102), "active",
        ))
        replies = [text async for text in self.plugin.memory_forget(
            self.event("/hindsight forget", mid="forget-after-upgrade", timestamp=103),
        )]
        self.assertIn("120 秒", str(replies))
        token = self.plugin.confirmations[scope["owner_id"]]["token"]
        replies = [text async for text in self.plugin.memory_forget(
            self.event("/hindsight forget " + token, mid="forget-after-upgrade-confirm", timestamp=104), token,
        )]
        self.assertIn("正在清空", str(replies))
        self.assertNotEqual(queue.scope(self.incoming)["bank"], scope["bank"])
    async def test_first_observation_of_old_event_after_bot_switch_is_rejected(self):
        old = self.event()
        self.bot.get_login_info.return_value = {"user_id": 1002}
        await self.plugin.prepare_sources()
        grant = self.plugin.outbox.access.authorization("qq", "2001")
        self.plugin.outbox.activate(replace(self.incoming, bot_id="1002"), grant["revision"])
        self.assertFalse(self.plugin.allowed(old))
        self.assertIsNone(self.plugin.command_scope(old))
        self.assertTrue(self.plugin.allowed(self.event(bot=1002)))

    async def test_offline_initialization_keeps_scope_disabled_without_throwing(self):
        self.context.get_platform_inst = lambda identifier: None
        await self.plugin.prepare_sources()
        self.plugin.refresh_permissions()
        self.assertFalse(self.plugin.allowed(self.event()))
        self.assertFalse(self.plugin.outbox.scope(self.incoming)["allowed"])

    async def test_replayed_pause_and_unseen_old_on_do_not_override_latest_mode(self):
        pause = self.event("/hindsight pause", mid="pause")
        self.assertIn("已暂停", await self.plugin.mode(pause, "paused"))
        self.assertIn("command_already_applied", await self.plugin.mode(pause, "paused"))
        with patch.object(time, "time", return_value=103.25):
            self.plugin.outbox.set_mode(self.plugin.outbox.scope(self.incoming)["scope_id"], "readonly")
        late = self.event("/hindsight on", mid="late", timestamp=102)
        self.assertIn("command_expired", await self.plugin.mode(late, "active"))
        self.assertEqual(self.plugin.outbox.scope(self.incoming)["mode"], "readonly")

    async def test_rebinding_during_async_mode_check_prevents_old_command(self):
        event = self.event("/hindsight on", mid="on")
        async def changed(**kwargs):
            self.bot.get_login_info.return_value = {"user_id": 1002}
            return True
        self.plugin.worker.maintenance.check = changed
        result = await self.plugin.mode(event, "active")
        self.assertIn("未开放", result)
        self.assertFalse(self.plugin.outbox.access.authorization("qq", "2001")["enabled"])

    async def test_same_client_login_change_rejects_first_old_command(self):
        event = self.event("/hindsight readonly")
        self.bot.get_login_info.return_value = {"user_id": 1002}
        self.assertIn("未开放", await self.plugin.mode(event, "readonly"))
        self.assertEqual(self.plugin.outbox.scope(self.incoming)["mode"], "active")
        self.assertFalse(self.plugin.outbox.access.authorization("qq", "2001")["enabled"])

    async def test_show_rechecks_login_after_private_read(self):
        async def read(bank, memory_id):
            self.bot.get_login_info.return_value = {"user_id": 1002}
            return {"text": "PRIVATE_TEST_CONTENT"}
        self.plugin.client = SimpleNamespace(ensure_auth=AsyncMock(), get_memory=read)
        results = [text async for text in self.plugin.memory_show(
            self.event("/hindsight show test"), "test",
        )]
        self.assertNotIn("PRIVATE_TEST_CONTENT", str(results))
        self.assertFalse(self.plugin.outbox.access.authorization("qq", "2001")["enabled"])

    async def test_manual_recall_uses_original_query_and_rechecks_authorization(self):
        self.plugin.recall_text = AsyncMock(return_value="found")
        event = self.event("/hindsight recall original full question")
        results = [text async for text in self.plugin.memory_recall(event, "truncated")]
        self.assertEqual(results, ["found"])
        self.assertEqual(self.plugin.recall_text.call_args.args[1], "original full question")
        async def revoked(*args, **kwargs):
            self.bot.get_login_info.return_value = {"user_id": 1002}
            return "PRIVATE_TEST_CONTENT"
        self.plugin.recall_text.side_effect = revoked
        results = [text async for text in self.plugin.memory_recall(event, "ignored")]
        self.assertNotIn("PRIVATE_TEST_CONTENT", str(results))
        calls = self.plugin.recall_text.await_count
        results = [text async for text in self.plugin.memory_recall(event, "ignored")]
        self.assertIn("未开放", str(results))
        self.assertEqual(self.plugin.recall_text.await_count, calls)

    async def test_panel_authorization_prepares_ingress_before_success(self):
        self.plugin.outbox.sync_permissions([])
        self.plugin.outbox.sync_permissions([("qq", "2001")])
        self.plugin.source_accounts.clear()
        panel = MemoryPanel(self.plugin)
        panel.directory = lambda: self.root
        payload = {
            "action": "prepare", "platform_instance_id": "qq", "user_id": "2001",
            "config_revision": panel.config_revision(),
        }
        with patch.dict(sys.modules, {"astrbot.api.web": SimpleNamespace(
            request=SimpleNamespace(username="admin"),
        )}):
            panel.body = AsyncMock(return_value=payload)
            prepared = await panel.authorize()
            panel.body = AsyncMock(return_value={
                **payload, "action": "confirm", "confirmation": prepared["confirmation"],
            })
            self.assertIn("已授权", (await panel.authorize())["message"])
        self.assertTrue(self.plugin.allowed(self.event()))
