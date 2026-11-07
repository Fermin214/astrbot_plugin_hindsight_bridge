"""Transport uncertainty and cancellation timing beyond deterministic Agent flows."""

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

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


RESPONSE = {"text": "Native synthesis from the cited fact.", "based_on": {
    "memories": [{"id": "fact-1", "type": "world", "document_id": "document-1"}],
    "mental_models": [], "directives": [],
}, "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}}


class ReflectTransport(unittest.IsolatedAsyncioTestCase):
    async def test_only_complete_native_results_or_definitely_unsent_transport_release_barriers(self):
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
            settled = Mock()
            value = await client.reflect("bank", "private-query", max_tokens=256, settled=settled)
            settled.assert_called_once()
            self.assertEqual(value, RESPONSE)
            body = json.loads(requests[0].content)
            self.assertEqual(body, {"query": "private-query", "budget": "low", "max_tokens": 256, "include": {"facts": {}}})
            self.assertEqual(requests[0].url.path, "/v1/default/banks/bank/reflect")
            for failure in ("read", "gateway", "invalid", "connect"):
                settled.reset_mock()
                with self.assertRaises(HindsightError):
                    await client.reflect("bank", "private-query", max_tokens=256, settled=settled)
                self.assertEqual(settled.call_count, int(failure == "connect"))
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

    async def ready(self, *, mode="automatic", enabled=True):
        event, _ = self.turn()
        scope = self.scope()
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

    async def test_independent_auto_mode_opens_only_reflect_and_uses_one_native_call(self):
        event, context, scope = await self.ready()
        self.assertEqual([tool.name for tool in self.request.func_tool.tools], [REFLECT_NAME])

        async def native(bank, query, *, max_tokens, settled):
            self.assertEqual(bank, scope["bank"])
            row = dict(self.plugin.outbox.control.reflection_pending(self.plugin.outbox.connection_id, bank=bank)[0])
            self.assertEqual(row["state"], "dispatched")
            self.assertNotIn(query, canonical(row))
            settled()
            return RESPONSE
        self.plugin.client.reflect = AsyncMock(side_effect=native)
        result = json.loads(await self.tools.execute(context, "reflect", {"query": "private-question"}))
        self.assertEqual(result["text"], RESPONSE["text"])
        self.assertEqual(result["usage"], RESPONSE["usage"])
        self.assertEqual(result["sources"]["memories"][0]["id"], "fact-1")
        self.assertEqual(self.request.func_tool.tools, [])
        self.assertFalse(self.plugin.outbox.control.reflection_pending(self.plugin.outbox.connection_id))
        self.assertIn("tool_context_unavailable", await self.tools.execute(context, "reflect", {"query": "again"}))
        self.plugin.client.reflect.assert_awaited_once()
        self.assertEqual(self.tools.turns[event].calls, 1)

    async def test_disabled_reflect_does_not_require_its_tool_for_existing_read_mode(self):
        self.tools.owned.pop(REFLECT_NAME)
        event, context, scope = await self.ready(mode="tools", enabled=False)
        self.assertEqual({tool.name for tool in self.request.func_tool.tools}, NAMES)
        self.assertTrue(self.tools.valid(self.tools.turns[event], event))
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
        self.assertTrue(self.plugin.outbox.control.reflection_pending(self.plugin.outbox.connection_id, bank=scope["bank"]))
        self.assertEqual(len(self.plugin.reflection.tasks), 1)
        release.set()
        await asyncio.gather(*list(self.plugin.reflection.tasks))
        self.assertFalse(self.plugin.outbox.control.reflection_pending(self.plugin.outbox.connection_id))
        self.assertEqual(self.plugin.hooks, 0)

    async def test_forget_waits_before_delete_and_drops_old_response(self):
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
        self.assertFalse(await maintenance.erase(record))
        client.delete_bank.assert_not_awaited()
        release.set()
        self.assertNotIn(RESPONSE["text"], await host)
        client.bank_exists.return_value = False
        self.assertTrue(await maintenance.erase(record))
        client.delete_bank.assert_awaited_once_with(scope["bank"])
        self.assertFalse(read_options(self.plugin.outbox, self.scope())["enabled"])

    async def test_unknown_request_survives_reload_blocks_old_erase_not_other_owners_recovery(self):
        _, context, scope = await self.ready()
        self.plugin.client.reflect = AsyncMock(side_effect=HindsightError("connection_error"))
        await self.tools.execute(context, "reflect", {"query": "lost"})
        queue = self.plugin.outbox
        record = queue.forget(scope["scope_id"])
        queue.close()
        self.plugin.outbox = Outbox(self.directory / "runtime", "test-service", "http://memory.test",
                                   credential_fingerprint=sha256("key"), control_directory=self.plugin.control_directory())
        queue = self.plugin.outbox
        self.assertEqual(queue.control.reflection_pending(queue.connection_id)[0]["state"], "unknown")
        self.plugin.client.delete_bank = AsyncMock()
        maintenance = Maintenance(queue, self.plugin.client)
        self.assertFalse(await maintenance.erase(record))
        self.plugin.client.delete_bank.assert_not_awaited()
        queue.recovering = True
        self.assertTrue(await maintenance.startup())
        self.assertFalse(queue.recovering)
        self.assertNotEqual(queue.control.erasures(queue.connection_id)[0]["phase"], "verified")
