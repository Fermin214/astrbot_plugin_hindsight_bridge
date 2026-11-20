import asyncio
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from plugins.astrbot_plugin_hindsight_bridge.core.client import HindsightClient, HindsightError
from plugins.astrbot_plugin_hindsight_bridge.core import diagnostics
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming, sha256
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox, QueueError
from plugins.astrbot_plugin_hindsight_bridge.core.permissions import pairs
from plugins.astrbot_plugin_hindsight_bridge.core.reference import reference_options, save_reference_options
from plugins.astrbot_plugin_hindsight_bridge.core.worker import Worker
from queue_fixture import confirm_sources


def directory_link(path, target):
    if os.name == "nt":
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(target)], check=True, capture_output=True)
    else:
        path.symlink_to(target, target_is_directory=True)


class Star:
    def __init__(self, context):
        self.context = context


class TextPart:
    def __init__(self, *, text):
        self.text = text
        self._no_save = False

    def mark_as_temp(self):
        self._no_save = True
        return self


class Plain:
    def __init__(self, text):
        self.text = text


class Request(SimpleNamespace):
    pass


class Configuration(dict):
    def save_config(self):
        pass


def bridge_class(directory, logger):
    def hook(*args, **kwargs):
        return lambda handler: handler

    def command_group(name):
        def register(handler):
            handler.command = hook
            return handler
        return register

    filters = SimpleNamespace(
        EventMessageType=SimpleNamespace(PRIVATE_MESSAGE="private"),
        event_message_type=hook, on_agent_begin=hook, on_agent_done=hook,
        on_llm_request=hook, command_group=command_group,
    )
    modules = {}
    for module_name, exports in {
        "astrbot.api": {"AstrBotConfig": Configuration, "logger": logger},
        "astrbot.api.event": {"AstrMessageEvent": object, "MessageChain": object, "filter": filters},
        "astrbot.api.message_components": {"File": object, "Plain": Plain},
        "astrbot.api.provider": {"ProviderRequest": Request},
        "astrbot.api.star": {"Context": object, "Star": Star,
                             "StarTools": SimpleNamespace(get_data_dir=lambda name: directory / "runtime")},
        "astrbot.core.agent.message": {"TextPart": TextPart},
        "astrbot.core.utils.astrbot_path": {"get_astrbot_data_path": lambda: str(directory)},
    }.items():
        stub = ModuleType(module_name)
        vars(stub).update(exports)
        modules[module_name] = stub
    source = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("plugins.astrbot_plugin_hindsight_bridge.test_main", source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    module.Worker = Mock(return_value=SimpleNamespace(run=AsyncMock()))
    return module.HindsightBridge


class ConfigurationTests(unittest.TestCase):
    def test_timeout_default_saved_values_and_invalid_ranges(self):
        self.assertEqual(diagnostics.recall_timeout({}), 10)
        for value in (0.2, 4, 10, 30, "12"):
            self.assertEqual(diagnostics.recall_timeout({"recall_timeout": value}), float(value))
        for value in (0, -1, 0.19, 31, True, None, [], "bad", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(diagnostics.ConfigurationError):
                diagnostics.recall_timeout({"recall_timeout": value})
        schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text())
        self.assertEqual(schema["recall_timeout"]["default"], 10)
        self.assertEqual(set(schema), {
            "api_url", "api_key", "control_plane_url", "service_id", "service_address_revision",
            "control_state_dir", "agent_id", "allowed_accounts", "recall_timeout",
            "recall_max_tokens", "recall_query_tokens", "query_tokenizer", "queue_max_tasks",
            "queue_max_mib", "command_prefixes", "context_max_chars",
            "background_max_tokens", "background_timeout",
        })

    def test_missing_fields_are_specific_and_secrets_are_not_displayed(self):
        config = {"api_url": "http://private-host", "api_key": "private-key", "service_id": " "}
        self.assertEqual(diagnostics.missing_connection(config), ["service_id"])
        message = diagnostics.configuration_message(config)
        self.assertIn("记忆服务识别码", message)
        self.assertNotIn("private", message)
        self.assertEqual(diagnostics.missing_connection({"api_url": None, "api_key": 12}),
                         ["api_url", "api_key", "service_id"])

    def test_warning_codes_have_independent_throttles_and_counts(self):
        logger = Mock()
        warnings = diagnostics.WarningLog(logger)
        with patch.object(diagnostics.time, "monotonic", side_effect=[0, 1, 2, 60]):
            warnings.warning("recall_timeout")
            warnings.warning("recall_timeout")
            warnings.warning("http_401")
            warnings.warning("recall_timeout")
        self.assertEqual(logger.warning.call_count, 3)
        self.assertEqual(logger.warning.call_args.args[-2:], (3, 2))
        warnings.recovered("recall_timeout")
        self.assertEqual(logger.info.call_args.args[-1], 3)
        self.assertNotIn("recall_timeout", warnings.errors)


class RuntimeDiagnostics(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.logger = Mock()
        self.config = Configuration({
            "api_url": "http://memory.test", "api_key": "key", "service_id": "test-service",
            "allowed_accounts": [{"platform_instance_id": "qq", "user_id": "123"}],
        })
        self.context = SimpleNamespace(
            get_config=lambda *args: {"agent_runner": {"runner_type": "local"}},
            conversation_manager=SimpleNamespace(
                get_conversation=AsyncMock(return_value=SimpleNamespace(
                    cid="conversation", user_id="private", history="[]",
                )),
                get_curr_conversation_id=AsyncMock(return_value="conversation"),
            ),
        )
        self.plugin = bridge_class(self.directory, self.logger)(self.context, self.config)
        self.plugin.outbox = Outbox(
            self.directory / "runtime", "test-service", "http://memory.test",
            credential_fingerprint=sha256("key"), control_directory=self.plugin.control_directory(),
        )
        self.plugin.worker = SimpleNamespace(wake=asyncio.Event())
        self.results = []
        self.requests = []
        self.plugin.client = HindsightClient("http://memory.test", "key", transport=httpx.MockTransport(self.service))
        self.plugin.allowed = lambda event: not self.plugin.stopping and self.enabled
        self.enabled = True
        self.plugin.snapshot = lambda event: self.incoming if self.plugin.allowed(event) else None
        self.incoming = Incoming("aiocqhttp", "qq", "456", "123", "one", int(time.time()), "hello")
        self.plugin.refresh_permissions()
        grant = self.plugin.outbox.access.authorization("qq", "123")
        self.plugin.outbox.activate(self.incoming, grant["revision"])

    async def asyncTearDown(self):
        self.plugin.stopping = True
        await self.plugin._close_resources()
        self.temporary.cleanup()

    async def service(self, request):
        self.requests.append(request)
        if request.headers.get("Authorization") != "Bearer key":
            return httpx.Response(401, json={})
        if request.url.path.endswith("/recall"):
            return httpx.Response(200, json={"results": self.results})
        return httpx.Response(200, json={"banks": []})

    def turn(self, message_id="one", conversation=True):
        self.incoming = Incoming("aiocqhttp", "qq", "456", "123", message_id, int(time.time()) + 1, "hello")
        event = type("Event", (), {"unified_msg_origin": "private"})()
        self.plugin.incoming[event] = self.incoming
        req = Request(
            conversation=SimpleNamespace(cid="conversation") if conversation else None,
            extra_user_content_parts=[], prompt="hello",
        )
        return event, req

    async def test_skip_diagnostics_rank_identity_and_time_before_shape_and_ignore_other_senders(self):
        plugin = self.plugin
        queue = plugin.outbox
        event = Mock()
        event.is_private_chat.return_value = True
        event.get_group_id.return_value = ""
        event.get_platform_name.return_value = "aiocqhttp"
        event.get_platform_id.return_value = "qq"
        event.get_sender_id.return_value = "123"
        shape = Mock(return_value="quoted_or_reply")
        timestamp = self.incoming.timestamp + 2

        def source_header(event, *, inspect=None):
            inspect(bot, timestamp, time.time(), shape)

        with patch.object(plugin, "source_header", side_effect=source_header) as header:
            event.get_sender_id.return_value = "stranger"
            plugin.record_skip(event, queue)
            event.get_sender_id.return_value = "123"
            event.get_group_id.return_value = "group"
            plugin.record_skip(event, queue)
            header.assert_not_called()
            self.assertEqual(plugin.skip_outcomes, {})
            event.get_group_id.return_value = ""
            bot = "new-bot"
            plugin.record_skip(event, queue)
            self.assertEqual(plugin.skip_status("qq", "123")["code"], "bot_not_confirmed")
            shape.assert_not_called()
            bot = "456"
            timestamp = self.incoming.timestamp - 1
            plugin.record_skip(event, queue)
            self.assertEqual(plugin.skip_status("qq", "123")["code"], "before_accept_from")
            shape.assert_not_called()
            timestamp = self.incoming.timestamp + 2
            plugin.record_skip(event, queue)
            outcome = plugin.skip_status("qq", "123")
            self.assertEqual(outcome["code"], "quoted_or_reply")
            self.assertEqual(set(plugin.skip_outcomes[("qq", "123")][-1]), {"code", "at"})
            self.assertEqual(queue.summary(self.incoming.scope(queue.deployment_id).id)["counts"], {})
            self.assertTrue(queue.access.observe_bot("qq", "aiocqhttp", "new-bot"))
            self.assertIsNone(plugin.skip_status("qq", "123"))
            grant = queue.access.authorization("qq", "123")
            queue.activate(replace(self.incoming, bot_id="new-bot"), grant["revision"])
            self.assertIsNone(plugin.skip_status("qq", "123"))
            self.config["allowed_accounts"] = []
            self.assertIsNone(plugin.skip_status("qq", "123"))

    async def test_observer_resuming_after_reload_cannot_record_a_skip(self):
        plugin = self.plugin
        queue = plugin.outbox
        event = Mock()
        event.get_platform_name.return_value = "aiocqhttp"
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare(platform):
            entered.set()
            await release.wait()

        with patch.object(plugin, "source_header", return_value=self.incoming), \
                patch.object(plugin, "prepare_sources", side_effect=prepare), \
                patch.object(plugin, "record_skip") as record:
            observation = asyncio.create_task(plugin.observe_incoming(event))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                plugin.outbox = object()
                release.set()
                await observation
                record.assert_not_called()
                self.assertEqual(plugin.skip_outcomes, {})
                self.assertNotIn(event, plugin.incoming)
            finally:
                plugin.outbox = queue
                if not observation.done():
                    observation.cancel()
                    await asyncio.gather(observation, return_exceptions=True)

    async def test_success_empty_and_failure_are_temporary_and_captured_durably(self):
        self.results = [{"id": "fact", "text": "coffee", "type": "world"}]
        event, req = self.turn()
        await self.plugin.add_memory(event, req)
        self.assertIn("插件已根据本轮对话自动检索", req.extra_user_content_parts[0].text)
        self.assertIn("coffee", req.extra_user_content_parts[0].text)
        self.assertTrue(req.extra_user_content_parts[0]._no_save)
        scope = self.plugin.outbox.scope(self.incoming)
        self.assertEqual(self.plugin.recall_status(scope)["outcome"], "success")
        self.results = []
        event, req = self.turn("two")
        await self.plugin.add_memory(event, req)
        self.assertTrue(req.extra_user_content_parts[0].text.endswith("Hindsight 检索完成，本轮未匹配到相关记忆。"))
        self.plugin.recall_text = AsyncMock(side_effect=HindsightError("recall_timeout"))
        for message_id in ("three", "four"):
            event, req = self.turn(message_id)
            await self.plugin.add_memory(event, req)
            self.assertTrue(req.extra_user_content_parts[0].text.endswith("Hindsight 记忆检索超时，已有记忆未载入。检索与新聊天保存分开执行，不能据此判断保存失败；保存结果以插件的保存进度为准。"))
        summary = self.plugin.recall_status(scope)
        self.assertEqual(summary["consecutive_failures"], 2)
        self.assertNotIn("hello", json.dumps(summary))
        rows = self.plugin.outbox.db.execute("SELECT payload,released FROM outbox").fetchall()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["released"] for row in rows))
        self.assertTrue(all("暂未载入" not in row["payload"] for row in rows))
        self.plugin.recall_text = AsyncMock(return_value="")
        event, req = self.turn("five")
        await self.plugin.add_memory(event, req)
        self.assertEqual(self.plugin.recall_status(scope)["consecutive_failures"], 0)
        self.logger.info.assert_called()

    async def test_automatic_recall_sends_user_budget_without_changing_token_limit(self):
        scope = self.plugin.outbox.scope(self.incoming)
        for budget in ("low", "mid", "high"):
            save_reference_options(self.plugin.outbox, scope, **{
                **reference_options(self.plugin.outbox, scope), "recall_budget": budget,
            })
            event, req = self.turn("budget-" + budget)
            await self.plugin.add_memory(event, req)
            request = next(request for request in reversed(self.requests) if request.url.path.endswith("/recall"))
            payload = json.loads(request.content)
            self.assertEqual((payload["budget"], payload["max_tokens"]), (budget, 1200))
        with self.assertRaisesRegex(HindsightError, "invalid_recall_budget"):
            await self.plugin.client.recall("bank", "hello", timestamp=self.incoming.iso_timestamp, budget="medium")

    async def test_disabling_assistant_capture_blocks_late_callback_and_future_backfill(self):
        plugin, queue = self.plugin, self.plugin.outbox
        event, request = self.turn("enabled-before-callback")
        await plugin.add_memory(event, request)
        queued_turn = plugin.pending_assistant_captures[event]
        scope = queue.scope(self.incoming)
        rows = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "delayed answer"}]
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_history(event, conversation):
            entered.set()
            await release.wait()
            return rows

        def settings(enabled):
            save_reference_options(queue, scope, **{
                **reference_options(queue, scope), "capture_assistant": enabled,
            })

        with patch.object(plugin, "conversation_history", side_effect=delayed_history):
            callback = asyncio.create_task(plugin.persist_history(event, queued_turn, "delayed answer"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                settings(False)
            finally:
                release.set()
                await asyncio.wait_for(callback, 2)
        self.assertEqual(queue.db.execute("SELECT count(*) FROM replies").fetchone()[0], 0)
        prior = queue.capture_assistant(queued_turn["task_id"], "already saved assistant context " + "字" * 4100)
        queue.release(prior.task_id)

        event, request = self.turn("disabled")
        with patch.object(plugin, "conversation_history", AsyncMock(return_value=rows)), \
                patch.object(plugin, "record_history", wraps=plugin.record_history) as backfill, \
                patch.object(plugin, "track_assistant_capture", wraps=plugin.track_assistant_capture) as track:
            await plugin.add_memory(event, request)
            backfill.assert_not_called()
            track.assert_not_called()
        anchor = queue.history_anchor(scope["scope_id"], "conversation")
        self.assertIsNone(anchor["history_before"])
        self.assertNotIn(event, plugin.pending_assistant_captures)
        self.assertEqual(queue.db.execute("SELECT released FROM outbox ORDER BY id").fetchall()[-1][0], 1)
        payload = json.loads(queue.db.execute("SELECT payload FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertNotIn("already saved assistant context", payload["items"][0]["content"])
        self.assertRegex(payload["items"][0]["content"], r"【历史语境】\n\[user;[^\n]+\]\nhello")

        settings(True)
        event, request = self.turn("reenabled")
        with patch.object(plugin, "conversation_history", AsyncMock(return_value=rows + rows)):
            await plugin.add_memory(event, request)
        self.assertIn(event, plugin.pending_assistant_captures)
        self.assertEqual(queue.db.execute("SELECT count(*) FROM replies").fetchone()[0], 1)
        self.assertEqual(queue.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 4)

    async def test_authentication_and_recall_deadlines_are_distinct_and_honor_setting(self):
        self.config["recall_timeout"] = 0.2
        async def stall(*args, **kwargs):
            await asyncio.sleep(1)
        self.plugin.client.ensure_auth = AsyncMock(side_effect=stall)
        with self.assertRaisesRegex(HindsightError, "auth_timeout"):
            await self.plugin.recall_text("bank", "hello", timestamp=self.incoming.iso_timestamp)
        self.plugin.client.ensure_auth = AsyncMock()
        self.plugin.client.recall = AsyncMock(side_effect=stall)
        with self.assertRaisesRegex(HindsightError, "recall_timeout"):
            await self.plugin.recall_text("bank", "hello", timestamp=self.incoming.iso_timestamp)
        self.config["recall_timeout"] = 10
        self.plugin.client.recall = AsyncMock(return_value=[])
        observed = []
        original = asyncio.timeout
        def record(seconds):
            observed.append(seconds)
            return original(seconds)
        with patch.object(asyncio, "timeout", side_effect=record):
            await self.plugin.recall_text("bank", "hello", timestamp=self.incoming.iso_timestamp)
        self.assertEqual(observed, [10])
        self.config["recall_timeout"] = 30
        with patch.object(asyncio, "timeout", side_effect=record):
            await self.plugin.recall_text("bank", "hello", timestamp=self.incoming.iso_timestamp)
        self.assertEqual(observed[-1], 30)

    async def test_history_failure_is_not_reported_as_recall_timeout(self):
        self.context.conversation_manager.get_curr_conversation_id.side_effect = TimeoutError
        event, req = self.turn(conversation=False)
        await self.plugin.add_memory(event, req)
        scope = self.plugin.outbox.scope(self.incoming)
        self.assertEqual(self.plugin.recall_status(scope)["code"], "history_timeout")
        self.assertIn("记忆准备失败", req.extra_user_content_parts[0].text)
        self.assertEqual(self.requests, [])

    async def test_full_capture_queue_preserves_custom_prompt_and_final_request(self):
        event, req = self.turn()
        queue = self.plugin.outbox
        scope = queue.scope(self.incoming)
        prompt = "  保留原文\n  "
        save_reference_options(queue, scope, **{**reference_options(queue, scope), "prompt": prompt})
        with patch.object(self.plugin, "freeze_user_capture", side_effect=QueueError("queue_capacity_reached")):
            await self.plugin.add_memory(event, req)
        turn, = queue.references.list(scope)
        delivery, = queue.references.detail(scope, turn["id"])["deliveries"]
        self.assertEqual((turn["conversation_id"], turn["message_id"]), ("conversation", self.incoming.message_id))
        self.assertEqual((delivery["prompt"], delivery["code"]), (prompt, "queue_capacity_reached"))
        self.assertEqual(delivery["text"], req.extra_user_content_parts[0].text)

    async def test_chat_auth_timeout_does_not_overwrite_a_newer_successful_request(self):
        self.config["recall_timeout"] = 0.2
        client = self.plugin.client
        client.auth_until = time.monotonic() + 60
        entered = asyncio.Event()

        async def stalled(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        client.ensure_auth = AsyncMock(side_effect=stalled)
        reading = asyncio.create_task(self.plugin.recall_text("bank", "hello", timestamp=self.incoming.iso_timestamp))
        try:
            await entered.wait()
            await client.request("GET", "/v1/version")
            with self.assertRaisesRegex(HindsightError, "auth_timeout"):
                await reading
            self.assertTrue(client.connection["connected"])
        finally:
            reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)

    async def test_revocation_maintenance_and_erasure_discard_late_content(self):
        for change in ("permission", "maintenance", "forget"):
            with self.subTest(change=change):
                self.config["allowed_accounts"] = [{"platform_instance_id": "qq", "user_id": "123"}]
                self.plugin.refresh_permissions()
                grant = self.plugin.outbox.access.authorization("qq", "123")
                if not grant["enabled"]:
                    self.plugin.outbox.activate(self.incoming, grant["revision"])
                scope = self.plugin.outbox.scope(self.incoming)
                async def result(*args, **kwargs):
                    if change == "permission":
                        self.config["allowed_accounts"] = []
                    elif change == "maintenance":
                        self.plugin.outbox.control.begin_maintenance("test-service", scope["owner_id"])
                        self.plugin.outbox.apply_control()
                    else:
                        self.plugin.outbox.forget(scope["scope_id"])
                    return "private memory"
                self.plugin.recall_text = AsyncMock(side_effect=result)
                event, req = self.turn(change)
                await self.plugin.add_memory(event, req)
                self.plugin.recall_text.assert_awaited_once()
                self.assertEqual(req.extra_user_content_parts, [], [part.text for part in req.extra_user_content_parts])
                if change == "maintenance":
                    self.plugin.outbox.control.finish_maintenance("test-service", scope["owner_id"])
                    self.plugin.outbox.apply_control()

    async def test_cancelled_turn_releases_queue_without_a_failure_notice(self):
        started = asyncio.Event()
        async def stall(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        self.plugin.recall_text = AsyncMock(side_effect=stall)
        event, req = self.turn()
        task = asyncio.create_task(self.plugin.add_memory(event, req))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(req.extra_user_content_parts, [])
        self.assertEqual(self.plugin.recall_outcomes, {})
        self.assertTrue(self.plugin.outbox.db.execute("SELECT released FROM outbox").fetchone()[0])
        self.assertEqual(self.plugin.hooks, 0)

    async def test_result_too_large_is_not_reported_as_no_match(self):
        self.plugin.client.recall = AsyncMock(return_value=[{"text": "x" * 10000}])
        with self.assertRaisesRegex(HindsightError, "recall_content_unavailable"):
            await self.plugin.recall_text("bank", "hello", timestamp=self.incoming.iso_timestamp)

    async def test_missing_configuration_and_invalid_timeout_open_no_resources(self):
        await self.plugin._close_resources()
        for config, label in (
            ({"api_url": "http://private", "api_key": "secret"}, "记忆服务识别码"),
            ({**self.config, "recall_timeout": 31}, "0.2 到 30"),
        ):
            self.plugin.config = config
            await self.plugin.initialize()
            self.assertIn(label, self.plugin.status)
            self.assertIsNone(self.plugin.client)
            self.assertIsNone(self.plugin.outbox)
            self.assertNotIn("secret", str(self.logger.mock_calls))

    async def test_control_paths_are_confined_before_initialization_opens_resources(self):
        await self.plugin._close_resources()
        root = self.plugin.control_directory()
        self.config["control_state_dir"] = str(root / "nested" / ".." / "journal")
        self.assertEqual(self.plugin.control_directory(), root / "journal")
        self.assertFalse((root / "journal").exists())
        outside = self.directory / "other-plugin"
        outside.mkdir(mode=0o755)
        directory_link(root / "escape", outside)
        directory_link(root / "inside", root)
        self.config["control_state_dir"] = str(root / "inside" / "journal")
        self.assertEqual(self.plugin.control_directory(), root / "journal")
        for path, code in (
            ("relative", "control_directory_must_be_absolute"),
            (str(outside), "control_directory_outside_plugin"),
            (str(root / ".." / "other-plugin"), "control_directory_outside_plugin"),
            (str(root) + "-suffix", "control_directory_outside_plugin"),
            (str(root / "escape" / "new"), "control_directory_outside_plugin"),
        ):
            self.config["control_state_dir"] = path
            await self.plugin.initialize()
            self.assertIn(code, self.plugin.status)
            self.assertIsNone(self.plugin.client)
            self.assertIsNone(self.plugin.outbox)
            self.assertEqual(list(outside.iterdir()), [])
            if os.name != "nt":
                self.assertEqual(outside.stat().st_mode & 0o777, 0o755)

    async def test_control_root_replaced_during_tokenizer_load_is_rejected_at_open(self):
        await self.plugin._close_resources()
        root = self.plugin.control_directory()
        outside = self.directory / "other-plugin"
        outside.mkdir(mode=0o755)
        async def replace_root(name):
            root.rename(root.with_name("original-control"))
            directory_link(root, outside)
        namespace = self.plugin.initialize.__func__.__globals__
        with patch.dict(namespace, load_encoding=replace_root):
            await self.plugin.initialize()
        self.assertIn("control_directory_outside_plugin", self.plugin.status)
        self.assertIsNone(self.plugin.outbox)
        self.assertIsNone(self.plugin.client)
        self.assertEqual(list(outside.iterdir()), [])
        if os.name != "nt":
            self.assertEqual(outside.stat().st_mode & 0o777, 0o755)

    async def test_default_control_allocation_is_independent_of_business_directory_alias(self):
        root = self.plugin.control_directory()
        namespace = self.plugin.control_root.__func__.__globals__
        with patch.dict(namespace, StarTools=SimpleNamespace(
            get_data_dir=lambda name: self.directory / "external-business",
        )):
            self.assertEqual(self.plugin.control_directory(), root)

    async def test_control_file_links_cannot_write_outside_the_allocated_root(self):
        await self.plugin._close_resources()
        root = self.plugin.control_directory()
        outside = self.directory / "external-directory"
        outside.mkdir()
        external = outside / "external-file"
        external.write_text("outside")
        external.chmod(0o644)
        namespace = self.plugin.initialize.__func__.__globals__
        for name in ("control.lock", "control.sqlite3", "control.sqlite3-wal", "control.sqlite3-shm"):
            directory = root / name.replace(".", "-")
            directory.mkdir()
            if os.name == "nt":
                directory_link(directory / name, outside)
            else:
                (directory / name).symlink_to(external)
            self.config["control_state_dir"] = str(directory)
            with patch.dict(namespace, load_encoding=AsyncMock()):
                await self.plugin.initialize()
            self.assertIn("control_directory_outside_plugin", self.plugin.status)
            self.assertIsNone(self.plugin.outbox)
            self.assertEqual(external.read_text(), "outside")
            self.assertEqual(list(outside.iterdir()), [external])
            if os.name != "nt":
                self.assertEqual(external.stat().st_mode & 0o777, 0o644)

    async def test_background_probe_retries_then_cancels_without_leaking_resources(self):
        await self.plugin.client.close()
        checked = asyncio.Event()
        async def probe(**kwargs):
            if self.plugin.client.check_connection.await_count == 1:
                raise HindsightError("connection_error")
            checked.set()
        self.plugin.client = SimpleNamespace(check_connection=AsyncMock(side_effect=probe), close=AsyncMock())
        sleep = asyncio.sleep
        async def quick_sleep(seconds):
            await sleep(0.001)
        with patch.object(asyncio, "sleep", side_effect=quick_sleep):
            self.plugin.connection_task = asyncio.create_task(self.plugin.monitor_connection())
            await asyncio.wait_for(checked.wait(), 1)
            self.plugin.stopping = True
            await self.plugin._close_resources()
        self.assertIsNone(self.plugin.connection_task)
        self.assertIsNone(self.plugin.client)
        self.logger.info.assert_called()

    async def test_initialize_schedules_auth_check_and_preserves_configured_wait(self):
        await self.plugin._close_resources()
        self.config["recall_timeout"] = 30
        factory = Mock(side_effect=lambda url, key, **kw: HindsightClient(
            url, key, transport=httpx.MockTransport(self.service), **kw,
        ))
        namespace = self.plugin.initialize.__func__.__globals__
        with patch.dict(namespace, HindsightClient=factory, load_encoding=AsyncMock(), Worker=Worker):
            await self.plugin.initialize()
        self.assertIsNotNone(self.plugin.connection_task)
        self.assertEqual(factory.call_args.kwargs["timeout"], 30)
        async def wait_connected():
            while self.plugin.client.connection["connected"] is not True:
                await asyncio.sleep(0.001)
        await asyncio.wait_for(wait_connected(), 1)
        self.assertEqual(len(self.requests), 3)
        self.assertFalse(self.plugin.outbox.recovering)

    async def test_upgrade_adds_account_template_marker_without_changing_identity_or_timeout(self):
        await self.plugin._close_resources()
        before = pairs(self.config["allowed_accounts"])
        self.config["recall_timeout"] = 4
        self.config.save_config = Mock()
        factory = Mock(side_effect=lambda url, key, **kw: HindsightClient(
            url, key, transport=httpx.MockTransport(self.service), **kw,
        ))
        namespace = self.plugin.initialize.__func__.__globals__
        with patch.dict(namespace, HindsightClient=factory, load_encoding=AsyncMock(), Worker=Worker):
            await self.plugin.initialize()
        self.assertEqual(self.config["allowed_accounts"][0]["__template_key"], "account")
        self.assertEqual(pairs(self.config["allowed_accounts"]), before)
        self.assertEqual(self.config["service_id"], "test-service")
        self.assertEqual(self.config["recall_timeout"], 4)
        self.config.save_config.assert_called_once()

    async def test_initial_offline_probe_does_not_block_durable_capture(self):
        await self.plugin._close_resources()
        async def offline(request):
            raise httpx.ConnectError("offline", request=request)
        factory = Mock(side_effect=lambda url, key, **kw: HindsightClient(
            url, key, transport=httpx.MockTransport(offline), **kw,
        ))
        namespace = self.plugin.initialize.__func__.__globals__
        with patch.dict(namespace, HindsightClient=factory, load_encoding=AsyncMock(), Worker=Worker):
            await self.plugin.initialize()
        async def wait_checked():
            while self.plugin.client.connection["connected"] is None or self.plugin.outbox.recovering:
                await asyncio.sleep(0.001)
        await asyncio.wait_for(wait_checked(), 1)
        event, req = self.turn()
        await self.plugin.add_memory(event, req)
        text = req.extra_user_content_parts[0].text
        self.assertTrue(text.endswith("Hindsight 连接失败，记忆暂未载入。"), text)
        self.assertTrue(self.plugin.outbox.db.execute("SELECT released FROM outbox").fetchone()[0])

    async def test_offline_restart_with_saved_auth_pause_still_captures_new_messages(self):
        self.plugin.outbox.scope(self.incoming)
        self.plugin.outbox.pause_connection("http_401")
        await self.plugin._close_resources()
        self.plugin.permissions = None
        async def offline(request):
            raise httpx.ConnectError("offline", request=request)
        factory = Mock(side_effect=lambda url, key, **kw: HindsightClient(
            url, key, transport=httpx.MockTransport(offline), **kw,
        ))
        namespace = self.plugin.initialize.__func__.__globals__
        with patch.dict(namespace, HindsightClient=factory, load_encoding=AsyncMock(), Worker=Worker):
            await self.plugin.initialize()
        async def wait_local_recovery():
            while self.plugin.outbox.recovering:
                await asyncio.sleep(0.001)
        await asyncio.wait_for(wait_local_recovery(), 1)
        self.assertEqual(self.plugin.outbox.connection_error(), "http_401")
        event, req = self.turn("after-restart")
        await self.plugin.add_memory(event, req)
        self.assertTrue(req.extra_user_content_parts[0].text.endswith("Hindsight 连接失败，记忆暂未载入。"))
        rows = self.plugin.outbox.db.execute("SELECT released,accepted FROM outbox").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["released"])
        self.assertFalse(rows[0]["accepted"])

    async def test_offline_restart_keeps_accepted_and_erasure_recovery_gates_closed(self):
        for blocked_by in ("accepted", "erasure"):
            with self.subTest(blocked_by=blocked_by):
                runtime = self.directory / blocked_by
                control = self.directory / (blocked_by + "-control")
                queue = Outbox(runtime, "test-service", "http://memory.test",
                               credential_fingerprint=sha256("key"), control_directory=control)
                try:
                    confirm_sources(queue, ("qq", "456", "123"))
                    incoming = replace(self.incoming, timestamp=int(time.time()) + 1)
                    scope = queue.scope(incoming)
                    if blocked_by == "accepted":
                        capture = queue.capture(incoming, "conversation")
                        queue.release(capture.task_id)
                        queue.update(capture.task_id, "tracking", accepted=1)
                    else:
                        queue.forget(scope["scope_id"])
                    queue.pause_connection("http_401")
                finally:
                    queue.close()
                queue = Outbox(runtime, "test-service", "http://memory.test",
                               credential_fingerprint=sha256("key"), control_directory=control)
                queue.recovering = True
                async def offline(request):
                    raise httpx.ConnectError("offline", request=request)
                client = HindsightClient("http://memory.test", "key", transport=httpx.MockTransport(offline))
                worker = Worker(queue, client, self.logger)
                task = asyncio.create_task(worker.run())
                try:
                    await asyncio.sleep(0.05)
                    self.assertTrue(queue.recovering)
                    self.assertFalse(queue.can_recall(queue.scope(self.incoming)))
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await client.close()
                    queue.close()


class ClientDiagnostics(unittest.IsolatedAsyncioTestCase):
    async def test_outer_probe_timeout_preserves_newer_evidence_and_records_its_own_failure(self):
        for newer_status in (None, 200, 401):
            with self.subTest(newer_status=newer_status):
                entered = asyncio.Event()

                async def service(request):
                    if request.url.path == "/newer":
                        return httpx.Response(newer_status, json={})
                    if request.headers.get("Authorization") == "Bearer key":
                        return httpx.Response(200, json={"banks": []})
                    entered.set()
                    await asyncio.Event().wait()

                client = HindsightClient("http://memory.test", "key", transport=httpx.MockTransport(service))
                client.auth_until = time.monotonic() + 60
                probe = asyncio.create_task(client.check_connection(force=True, timeout=0.05))
                try:
                    await entered.wait()
                    if newer_status == 200:
                        await client.request("GET", "/newer")
                    elif newer_status == 401:
                        with self.assertRaises(HindsightError):
                            await client.request("GET", "/newer")
                    with self.assertRaisesRegex(HindsightError, "auth_timeout"):
                        await probe
                    self.assertEqual(client.connection["connected"], newer_status == 200)
                    self.assertEqual(client.connection["code"], "" if newer_status == 200 else "http_401" if newer_status else "auth_timeout")
                finally:
                    probe.cancel()
                    await asyncio.gather(probe, return_exceptions=True)
                    await client.close()

    async def test_late_success_cannot_erase_newer_connection_failure(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def service(request):
            if request.url.path == "/slow":
                entered.set()
                await release.wait()
                return httpx.Response(200, json={})
            raise httpx.ConnectError("offline", request=request)

        client = HindsightClient("http://memory.test", "key", transport=httpx.MockTransport(service))
        client.auth_until = time.monotonic() + 60
        older = asyncio.create_task(client.request("GET", "/slow"))
        try:
            await entered.wait()
            with self.assertRaises(HindsightError):
                await client.request("GET", "/newer")
            release.set()
            await older
            self.assertFalse(client.connection["connected"])
            self.assertEqual(client.connection["code"], "connection_error")
        finally:
            release.set()
            await older
            await client.close()

    async def test_streamed_auth_failure_and_recovery_update_connection_but_missing_content_does_not(self):
        status = 401

        async def service(request):
            return httpx.Response(status, json={})

        client = HindsightClient("http://memory.test", "key", transport=httpx.MockTransport(service))
        client.auth_until = time.monotonic() + 60
        try:
            with self.assertRaises(HindsightError):
                await client.read_bounded("/page")
            self.assertFalse(client.connection["connected"])
            status = 200
            await client.read_bounded("/page")
            self.assertTrue(client.connection["connected"])
            status = 404
            with self.assertRaises(HindsightError):
                await client.read_bounded("/page")
            self.assertTrue(client.connection["connected"])
        finally:
            await client.close()

    async def test_recall_wait_overrides_http_timeout_without_changing_queue_defaults(self):
        observed = []
        async def service(request):
            observed.append(request.extensions["timeout"]["read"])
            if request.headers.get("Authorization") != "Bearer key":
                return httpx.Response(401, json={})
            return httpx.Response(200, json={"results": []})
        client = HindsightClient("http://memory.test", "key", transport=httpx.MockTransport(service))
        try:
            await client.recall("bank", "hello", timestamp="2026-11-03T12:00:00Z", timeout=30)
            self.assertEqual(observed, [30] * 4)
            self.assertEqual(client.http.timeout.read, 15)
        finally:
            await client.close()

    async def test_transport_failures_keep_safe_distinct_codes(self):
        for error, code in (
            (httpx.ConnectTimeout, "connect_timeout"), (httpx.ReadTimeout, "read_timeout"),
            (httpx.WriteTimeout, "write_timeout"), (httpx.PoolTimeout, "pool_timeout"),
            (httpx.ConnectError, "connection_error"),
        ):
            async def fail(request):
                raise error("private server data", request=request)
            client = HindsightClient("http://memory.test", "key", transport=httpx.MockTransport(fail))
            try:
                with self.assertRaisesRegex(HindsightError, "^" + code + "$"):
                    await client.request("GET", "/v1/default/banks")
                self.assertFalse(client.connection["connected"])
                self.assertEqual(client.connection["code"], code)
            finally:
                await client.close()

    async def test_overlapping_auth_probes_share_one_validation(self):
        count = 0
        async def service(request):
            nonlocal count
            count += 1
            await asyncio.sleep(0.01)
            return httpx.Response(200 if request.headers.get("Authorization") == "Bearer key" else 401,
                                  json={"banks": []})
        client = HindsightClient("http://memory.test", "key", transport=httpx.MockTransport(service))
        try:
            await asyncio.gather(client.check_connection(force=True), client.check_connection(force=True))
            self.assertEqual(count, 3)
            self.assertTrue(client.connection["connected"])
        finally:
            await client.close()
