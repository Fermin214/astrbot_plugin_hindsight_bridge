"""Native Reflect failures, startup archival and late-result isolation."""

import asyncio
from dataclasses import replace
import json
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx

import test_background_hooks as hooks
import test_reading as reading_tests
import test_runtime_diagnostics as runtime
from plugins.astrbot_plugin_hindsight_bridge.core.agent_tools import NAMES, REFLECT_NAME, save_options
from plugins.astrbot_plugin_hindsight_bridge.core.client import HindsightClient, HindsightError
from plugins.astrbot_plugin_hindsight_bridge.core.identity import canonical, sha256
from plugins.astrbot_plugin_hindsight_bridge.core.maintenance import Maintenance
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox
from plugins.astrbot_plugin_hindsight_bridge.core.reflection import read_options, save_options as save_reflect
from plugins.astrbot_plugin_hindsight_bridge.core.reference import reference_options, save_reference_options


RESPONSE = {"text": "Native synthesis from the cited fact.", "based_on": {
    "memories": [{"id": "fact-1", "type": "world", "document_id": "document-1"}],
    "mental_models": [], "directives": [],
}, "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}}


class ReflectMigration(unittest.TestCase):
    def test_startup_archives_unfinished_requests_without_changing_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = ("request", "connection", "owner", "bank", 3, "unknown", 123.5, "disconnect")
            def open_queue():
                return Outbox(root, "fixture", "http://memory.test", credential_fingerprint="test")
            with closing(open_queue()) as queue, queue.db:
                queue.db.execute("INSERT INTO reflection_requests VALUES (?,?,?,?,?,?,?,?,NULL,'')", original)
            with closing(open_queue()) as queue:
                control = queue.control
                self.assertEqual(control.db.execute("PRAGMA user_version").fetchone()[0], 8)
                audit = control.db.execute("SELECT * FROM reflection_requests").fetchone()
                self.assertEqual(tuple(audit)[:8], (*original[:5], "overridden", *original[6:]))
                self.assertIsInstance(audit["overridden_at"], float)
                self.assertEqual(audit["overridden_by"], "startup")
                with control.transaction():
                    for state in ("prepared", "dispatched", "unknown"):
                        control.db.execute("INSERT INTO reflection_requests(request_id,connection_id,owner_id,bank,generation,state,created_at) "
                                           "VALUES (?,?,?,?,?,?,?)", (state, "connection", "owner", state, 3, state, 124))
            with closing(open_queue()) as queue:
                control = queue.control
                self.assertEqual(tuple(control.db.execute("SELECT * FROM reflection_requests WHERE request_id='request'").fetchone()), tuple(audit))
                self.assertEqual(control.db.execute("SELECT COUNT(*) FROM reflection_requests WHERE state='overridden'").fetchone()[0], 4)



class ReflectTransport(unittest.IsolatedAsyncioTestCase):
    async def test_native_reflect_payload_and_transport_failures(self):
        requests = []
        failure = None

        async def service(request):
            requests.append(request)
            if failure == "read":
                raise httpx.ReadError("synthetic disconnect", request=request)
            if failure == "connect":
                raise httpx.ConnectError("synthetic connect failure", request=request)
            if failure == "gateway":
                return httpx.Response(504, json={"detail": "synthetic proxy timeout"})
            if failure == "invalid":
                return httpx.Response(200, json={"text": 1})
            return httpx.Response(200, json=RESPONSE)

        client = HindsightClient("http://memory.test", "key", transport=httpx.MockTransport(service))
        try:
            settled = lambda: None
            value = await client.reflect("bank", "private-query", max_tokens=256, settled=settled)
            self.assertEqual(value, RESPONSE)
            body = json.loads(requests[0].content)
            self.assertEqual(body, {"query": "private-query", "budget": "low", "max_tokens": 256, "include": {"facts": {}}})
            self.assertEqual(requests[0].url.path, "/v1/default/banks/bank/reflect")
            for failure in ("read", "gateway", "invalid", "connect"):
                with self.assertRaises(HindsightError):
                    await client.reflect("bank", "private-query", max_tokens=256, settled=settled)
        finally:
            await client.close()


class ReflectBoundaries(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = runtime.RuntimeDiagnostics.asyncTearDown
    turn = runtime.RuntimeDiagnostics.turn
    service = hooks.BackgroundHooks.service
    scope = hooks.BackgroundHooks.scope

    async def asyncSetUp(self):
        await runtime.RuntimeDiagnostics.asyncSetUp(self)
        self.tools = self.plugin.read_tools
        self.tools.owned = {name: SimpleNamespace(name=name, description=name, active=True,
                            handler_module_path="bridge.main") for name in NAMES | {REFLECT_NAME}}
        self.tools.manager = SimpleNamespace(get_func=lambda name: self.tools.owned.get(name), remove_func=lambda name: None)
        self.context.get_using_provider_async = AsyncMock(return_value=SimpleNamespace(
            provider_config={"id": "fixture", "modalities": ["tool_use"]}, get_model=lambda: "fixture",
        ))
        self.plugin.prepare_sources = AsyncMock()
        self.plugin.client.ensure_auth = AsyncMock()
        self.plugin.client.bank_exists = AsyncMock(return_value=True)

    async def ready(self, *, mode="automatic", enabled=True, readonly=False):
        event, _ = self.turn()
        scope = self.scope()
        if readonly:
            self.incoming = replace(self.incoming, capture_allowed=False)
            self.plugin.incoming[event] = self.incoming
            save_reference_options(self.plugin.outbox, scope, **{
                **reference_options(self.plugin.outbox, scope), "recall_on_quoted_or_link": True,
            })
        if mode == "tools":
            save_options(self.plugin.outbox, scope, revision=0, mode=mode)
        if enabled:
            save_reflect(self.plugin.outbox, scope, revision=0, enabled=True, timeout=3)
        self.request = reading_tests.ToolRequest()
        self.request.model, self.request.prompt = None, "source"
        self.request.func_tool = reading_tests.ToolSet(list(self.tools.owned.values()))
        original = self.tools.filter_request(event, self.request)
        await self.tools.prepare(event, self.request, self.incoming, scope, original)
        context = reading_tests.RunContext()
        context.context = SimpleNamespace(context=self.context, event=event)
        context.messages = [SimpleNamespace(role="user", content=[
            SimpleNamespace(text="source", _no_save=False),
            SimpleNamespace(text=self.tools.request_prefix(event) + "notice", _no_save=True),
        ])]
        self.tools.begin(event, context)
        return event, context, scope

    async def test_readonly_reflect_cannot_run_after_bot_identity_changes(self):
        event, context, scope = await self.ready(readonly=True)
        self.assertFalse(self.incoming.capture_allowed)
        self.plugin.outbox.access.observe_bot("qq", "aiocqhttp", "another-bot")
        native = self.plugin.client.reflect = AsyncMock()
        self.assertIn("reading_changed", await self.tools.execute(context, "reflect", {"query": "q"}))
        native.assert_not_awaited()
        self.assertFalse(self.plugin.reflection.tasks)

    async def test_independent_auto_mode_opens_only_reflect_and_uses_one_native_call(self):
        event, context, scope = await self.ready()
        self.assertEqual([tool.name for tool in self.request.func_tool.tools], [REFLECT_NAME])

        async def native(bank, query, *, max_tokens, settled):
            self.assertEqual(bank, scope["bank"])
            self.assertEqual(len(self.plugin.reflection.tasks), 1)
            self.assertEqual(self.plugin.outbox.control.db.execute("SELECT COUNT(*) FROM reflection_requests").fetchone()[0], 0)
            settled()
            return RESPONSE
        self.plugin.client.reflect = AsyncMock(side_effect=native)
        result = json.loads(await self.tools.execute(context, "reflect", {"query": "private-question"}))
        self.assertEqual(result["text"], RESPONSE["text"])
        self.assertEqual(result["usage"], RESPONSE["usage"])
        self.assertEqual(result["sources"]["memories"][0]["id"], "fact-1")
        self.assertEqual(self.request.func_tool.tools, [])
        self.assertFalse(self.plugin.reflection.tasks)
        self.assertIn("tool_context_unavailable", await self.tools.execute(context, "reflect", {"query": "again"}))
        self.plugin.client.reflect.assert_awaited_once()
        self.assertEqual(self.tools.tool_turns[event].calls, 1)

    async def test_disabled_reflect_does_not_require_its_tool_for_existing_read_mode(self):
        self.tools.owned.pop(REFLECT_NAME)
        event, context, scope = await self.ready(mode="tools", enabled=False)
        self.assertEqual({tool.name for tool in self.request.func_tool.tools}, NAMES)
        self.assertTrue(self.tools.valid(self.tools.tool_turns[event], event))
        self.assertFalse(read_options(self.plugin.outbox, scope)["enabled"])

    async def test_user_deadline_keeps_native_request_tracked_until_response(self):
        _, context, scope = await self.ready()
        entered, release = asyncio.Event(), asyncio.Event()

        async def native(*args, settled, **kwargs):
            entered.set()
            await release.wait()
            settled()
            return RESPONSE
        self.plugin.client.reflect = AsyncMock(side_effect=native)
        host = asyncio.create_task(self.tools.execute(context, "reflect", {"query": "slow"}))
        await entered.wait()
        result = json.loads(await host)
        self.assertEqual(result["error"], "reflect_timeout")
        self.assertEqual(len(self.plugin.reflection.tasks), 1)
        self.assertEqual(self.plugin.outbox.control.db.execute("SELECT COUNT(*) FROM reflection_requests").fetchone()[0], 0)
        release.set()
        await asyncio.gather(*list(self.plugin.reflection.tasks))
        self.assertFalse(self.plugin.reflection.tasks)
        self.assertEqual(self.plugin.hooks, 0)

    async def test_resume_preserves_reflect_and_reopens_only_after_platform_second_boundary(self):
        _, _, scope = await self.ready()
        queue = self.plugin.outbox
        queue.set_mode(scope["scope_id"], "readonly", now=int(time.time()))
        self.plugin.client.list_operations = AsyncMock(return_value=[])
        maintenance = Maintenance(queue, self.plugin.client)
        maintenance.begin(scope["scope_id"])
        resumed_at = int(time.time()) + 4.25
        with unittest.mock.patch("plugins.astrbot_plugin_hindsight_bridge.core.control.time.time", return_value=resumed_at):
            await maintenance.resume(scope["scope_id"])
        current = queue.scope_owner(scope["scope_id"])
        boundary = current["accept_from"]
        self.assertGreater(boundary, resumed_at)
        self.assertTrue(read_options(queue, current)["enabled"])
        shared = reading_tests.ToolSet(list(self.tools.owned.values()))
        for accepted in (False, True):
            event, request = self.turn("after-resume-" + str(accepted))
            self.incoming = replace(self.incoming, timestamp=boundary if accepted else boundary - 1,
                                    received_at=boundary + 0.1 if accepted else resumed_at + 0.1)
            self.plugin.incoming[event] = self.incoming
            request.model, request.func_tool = None, shared
            await self.plugin.add_memory(event, request)
            self.assertEqual(REFLECT_NAME in {tool.name for tool in request.func_tool.tools}, accepted)
            self.assertIn(REFLECT_NAME, {tool.name for tool in shared.tools})
            self.assertTrue(read_options(queue, queue.scope_owner(scope["scope_id"]))["enabled"])
            if not accepted:
                outcome = self.plugin.recall_outcomes[(queue.connection_id, scope["owner_id"])]
                self.assertEqual(outcome["code"], "message_before_accept_from")

    async def test_forget_deletes_without_waiting_for_reflect_and_drops_old_response(self):
        _, context, scope = await self.ready()
        entered, release = asyncio.Event(), asyncio.Event()

        async def native(*args, settled, **kwargs):
            entered.set()
            await release.wait()
            settled()
            return RESPONSE
        client = self.plugin.client
        client.reflect = AsyncMock(side_effect=native)
        client.delete_bank, client.list_operations, client.bank_list_contains = AsyncMock(), AsyncMock(return_value=[]), AsyncMock(return_value=False)
        host = asyncio.create_task(self.tools.execute(context, "reflect", {"query": "race"}))
        await entered.wait()
        record = self.plugin.outbox.forget(scope["scope_id"])
        maintenance = Maintenance(self.plugin.outbox, client)
        client.bank_exists.return_value = False
        self.assertTrue(await maintenance.erase(record))
        client.delete_bank.assert_awaited_once_with(scope["bank"])
        release.set()
        self.assertNotIn(RESPONSE["text"], await host)
        self.assertNotEqual(self.plugin.outbox.control.erasures(self.plugin.outbox.connection_id)[0]["phase"], "verified")
        self.assertTrue(await maintenance.erase(record))
        self.assertFalse(read_options(self.plugin.outbox, self.scope())["enabled"])

    async def test_disconnected_reflect_does_not_block_reload_or_erasure(self):
        _, context, scope = await self.ready()
        self.plugin.client.reflect = AsyncMock(side_effect=HindsightError("connection_error"))
        await self.tools.execute(context, "reflect", {"query": "lost"})
        queue = self.plugin.outbox
        record = queue.forget(scope["scope_id"])
        queue.close()
        self.plugin.outbox = Outbox(self.directory / "runtime", "test-service", "http://memory.test",
                                   credential_fingerprint=sha256("key"), control_directory=self.plugin.control_directory())
        queue = self.plugin.outbox
        self.assertEqual(queue.control.db.execute("SELECT COUNT(*) FROM reflection_requests").fetchone()[0], 0)
        self.plugin.client.delete_bank = AsyncMock()
        self.plugin.client.list_operations = AsyncMock(return_value=[])
        self.plugin.client.bank_exists = AsyncMock(return_value=False)
        self.plugin.client.bank_list_contains = AsyncMock(return_value=False)
        maintenance = Maintenance(queue, self.plugin.client)
        self.assertTrue(await maintenance.erase(record))
        self.plugin.client.delete_bank.assert_awaited_once_with(scope["bank"])
        queue.recovering = True
        self.assertTrue(await maintenance.startup())
        self.assertFalse(queue.recovering)
        self.assertEqual(queue.control.erasures(queue.connection_id)[0]["phase"], "verified")

    async def test_disconnected_reflect_allows_a_new_request(self):
        _, context, scope = await self.ready()
        control = self.plugin.outbox.control

        self.plugin.client.reflect = AsyncMock(side_effect=HindsightError("connection_error"))
        await self.tools.execute(context, "reflect", {"query": "first"})
        _, context, _ = await self.ready(enabled=False)

        async def completed(*args, settled, **kwargs):
            settled()
            return RESPONSE

        self.plugin.client.reflect.side_effect = completed
        result = json.loads(await self.tools.execute(context, "reflect", {"query": "next"}))
        self.assertEqual(result["text"], RESPONSE["text"])
        self.assertFalse(self.plugin.reflection.tasks)
        self.assertEqual(control.db.execute("SELECT COUNT(*) FROM reflection_requests").fetchone()[0], 0)
