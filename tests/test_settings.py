"""Settings races and durable-queue boundaries that browser flows cannot time exactly."""

import asyncio
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from plugins.astrbot_plugin_hindsight_bridge.core import settings
from plugins.astrbot_plugin_hindsight_bridge.core.background import reference
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import QueueError
from plugins.astrbot_plugin_hindsight_bridge.core.panel import PanelError

import test_background_hooks as background


class ParameterSettings(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = background.BackgroundHooks.asyncSetUp
    asyncTearDown = background.BackgroundHooks.asyncTearDown
    service = background.BackgroundHooks.service
    turn = background.BackgroundHooks.turn
    scope = background.BackgroundHooks.scope
    select = background.BackgroundHooks.select
    panel = background.BackgroundHooks.panel

    def setup_panel(self):
        panel = self.panel(SimpleNamespace(username="admin"))
        panel.body = AsyncMock(return_value={
            "config_revision": panel.config_revision(),
            "parameters": settings.values(self.config),
        })
        return panel

    async def test_token_only_panel_uses_the_internal_character_ceiling(self):
        panel = self.setup_panel()
        payload = panel.body.return_value["parameters"]
        payload.pop("context_max_chars")
        result = await panel.save_parameters()
        self.assertTrue(result["saved"])
        self.assertEqual(self.config["context_max_chars"], 32768)
        self.assertEqual(result["parameters"]["effective"]["recall_max_tokens"], payload["recall_max_tokens"])

    async def test_save_keeps_queue_payload_selection_pause_and_identity_and_applies_capacity(self):
        self.select()
        event, req = self.turn()
        await self.plugin.add_memory(event, req)
        queue, client = self.plugin.outbox, self.plugin.client
        scope = self.scope()
        queue.control.begin_maintenance(queue.connection_id, scope["owner_id"])
        before = [tuple(row) for row in queue.db.execute("SELECT * FROM outbox")]
        selection = reference(queue, scope)
        owner = queue.control.owner(queue.connection_id, scope["owner_id"])
        panel = self.setup_panel()
        parameters = panel.body.return_value["parameters"]
        parameters.update(queue_max_tasks=1, queue_max_mib=2, background_max_tokens=4096,
                          recall_max_tokens=6000, context_max_chars=24000, recall_query_tokens=32)
        self.config.save_config = Mock()
        result = await panel.save_parameters()
        self.assertTrue(result["saved"] and result["parameters"]["running"])
        self.assertIs(self.plugin.outbox, queue)
        self.assertIs(self.plugin.client, client)
        self.assertEqual(result["parameters"]["values"], parameters)
        self.assertEqual(result["parameters"]["effective"], parameters)
        self.assertEqual([tuple(row) for row in queue.db.execute("SELECT * FROM outbox")], before)
        self.assertEqual(reference(queue, scope), selection)
        self.assertEqual(queue.control.owner(queue.connection_id, scope["owner_id"]), owner)
        self.assertTrue(queue.control.maintenance(queue.connection_id, scope["bank"]))
        self.assertEqual(queue.max_tasks, 1)
        self.assertEqual(queue.max_bytes, 2 * 1024 * 1024)
        queue.control.finish_maintenance(queue.connection_id, scope["owner_id"])
        queue.apply_control()
        self.turn("capacity")
        with self.assertRaisesRegex(QueueError, "queue_capacity_reached"):
            queue.capture(self.incoming, "conversation")
        self.assertEqual([tuple(row) for row in queue.db.execute("SELECT * FROM outbox")], before)
        self.config.save_config.assert_called_once()

    async def test_save_during_read_discards_the_old_result_and_preserves_frozen_capture(self):
        self.select()
        panel = self.setup_panel()
        panel.body.return_value["parameters"].update(background_max_tokens=4096)
        entered, release = asyncio.Event(), asyncio.Event()

        async def gated():
            entered.set()
            await release.wait()

        self.page_gate = gated
        event, req = self.turn()
        reading = asyncio.create_task(self.plugin.add_memory(event, req))
        await entered.wait()
        try:
            before = self.plugin.outbox.db.execute("SELECT payload FROM outbox").fetchone()[0]
            await panel.save_parameters()
        finally:
            release.set()
            await reading
        self.assertEqual(req.extra_user_content_parts, [])
        row = self.plugin.outbox.db.execute("SELECT payload,state FROM outbox").fetchone()
        self.assertEqual(row["payload"], before)
        self.assertEqual(row["state"], "ready")

    async def test_live_byte_capacity_rejects_new_material_and_reopens_after_increase(self):
        panel = self.setup_panel()
        panel.body.return_value["parameters"]["queue_max_mib"] = 1
        await panel.save_parameters()
        queue = self.plugin.outbox
        for number in range(200):
            self.turn("byte-capacity-" + str(number))
            self.incoming = self.incoming.__class__(
                **{**self.incoming.__dict__, "text": "界" * 7000},
            )
            try:
                queue.capture(self.incoming, "conversation")
            except QueueError as exc:
                self.assertEqual(str(exc), "queue_capacity_reached")
                break
        else:
            self.fail("one MiB limit did not reject new material")
        before = [tuple(row) for row in queue.db.execute("SELECT * FROM outbox")]
        panel.body.return_value.update(config_revision=panel.config_revision())
        panel.body.return_value["parameters"]["queue_max_mib"] = 2
        await panel.save_parameters()
        self.assertTrue(queue.capture(self.incoming, "conversation").created)
        self.assertEqual([tuple(row) for row in queue.db.execute("SELECT * FROM outbox ORDER BY id")][:-1], before)

    async def test_stale_revision_after_tokenizer_load_cannot_overwrite_external_changes(self):
        panel = self.setup_panel()
        original = copy.deepcopy(dict(self.config))
        entered, release = asyncio.Event(), asyncio.Event()

        async def gated(name):
            entered.set()
            await release.wait()

        with patch.object(settings, "load_encoding", side_effect=gated):
            saving = asyncio.create_task(panel.save_parameters())
            await entered.wait()
            self.config["queue_max_tasks"] = 9
            release.set()
            with self.assertRaisesRegex(PanelError, "配置已被更新"):
                await saving
        self.assertEqual(dict(self.config), {**original, "queue_max_tasks": 9})
        self.assertEqual(self.plugin.outbox.max_tasks, 10000)

    async def test_concurrent_saves_accept_only_one_revision_and_reject_identity_fields(self):
        panel = self.setup_panel()
        first, second = await asyncio.gather(panel.save_parameters(), panel.save_parameters(), return_exceptions=True)
        self.assertTrue(first["saved"])
        self.assertIsInstance(second, PanelError)
        panel.body.return_value.update(config_revision=panel.config_revision(), service_id="other")
        with self.assertRaisesRegex(PanelError, "只提交记忆参数"):
            await panel.save_parameters()
        panel.body.return_value.pop("service_id")
        panel.body.return_value["parameters"]["agent_id"] = "other"
        with self.assertRaisesRegex(ValueError, "未知项目"):
            await panel.save_parameters()

    async def test_failed_disk_write_restores_values_without_applying_runtime_changes(self):
        panel = self.setup_panel()
        panel.body.return_value["parameters"].update(queue_max_tasks=1)
        before = dict(self.config)
        disk = []

        def persist():
            disk.append(json.dumps(self.config, sort_keys=True))
            if len(disk) == 1:
                raise OSError("disk full")

        self.config.save_config = persist
        with self.assertRaisesRegex(PanelError, "保存失败"):
            await panel.save_parameters()
        self.assertEqual(dict(self.config), before)
        self.assertEqual(json.loads(disk[-1]), before)
        self.assertEqual(self.plugin.outbox.max_tasks, 10000)

    async def test_malformed_parameters_fail_before_loading_or_saving(self):
        panel = self.setup_panel()
        defaults = settings.values(self.config)
        self.config.save_config = Mock()
        with patch.object(settings, "load_encoding", new_callable=AsyncMock) as load:
            for key, value in (
                ("recall_timeout", True), ("background_timeout", float("nan")),
                ("recall_max_tokens", 8193), ("context_max_chars", "4000"),
                ("recall_query_tokens", 401), ("queue_max_tasks", 0),
                ("queue_max_mib", 1.2), ("query_tokenizer", "chat-model-name"),
                ("command_prefixes", [""]), ("command_prefixes", ["x\ny"]),
            ):
                with self.subTest(key=key, value=value):
                    panel.body.return_value["parameters"] = {**defaults, key: value}
                    with self.assertRaises(ValueError):
                        await panel.save_parameters()
            load.assert_not_awaited()
        self.config.save_config.assert_not_called()
