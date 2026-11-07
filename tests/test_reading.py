"""Boundaries that are difficult to force deterministically through the Agent."""

import asyncio
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import test_runtime_diagnostics as runtime
import test_background_hooks as hooks
from plugins.astrbot_plugin_hindsight_bridge.core import reading
from plugins.astrbot_plugin_hindsight_bridge.core.agent_tools import NAMES, read_options, save_options
from plugins.astrbot_plugin_hindsight_bridge.core.client import BACKGROUND_MAX_BYTES, HindsightClient, HindsightError
from plugins.astrbot_plugin_hindsight_bridge.core.control import QueueError
from plugins.astrbot_plugin_hindsight_bridge.core.identity import canonical


class ReadContracts(unittest.IsolatedAsyncioTestCase):
    async def test_history_array_is_bounded_before_projection_and_does_not_expose_failure_details(self):
        rows = [{"changed_at": "2026-11-07T00:00:00+00:00", "kind": "refresh_failed",
                 "previous_content": None, "error_message": "private provider key"}]

        async def service(request):
            return httpx.Response(200, json=rows)
        client = HindsightClient("http://memory.test", "key", transport=httpx.MockTransport(service))
        try:
            result = await reading.page_history(client, "bank", {"mental_model_id": "mm-1"})
            self.assertFalse(result["complete_history"])
            self.assertFalse(result["items"][0]["has_new_version"])
            self.assertNotIn("private provider key", canonical(result))
            rows[:] = [{"changed_at": "2026-11-07T00:00:00+00:00", "previous_content": "x" * BACKGROUND_MAX_BYTES}]
            with self.assertRaisesRegex(HindsightError, "history_response_too_large"):
                await reading.page_history(client, "bank", {"mental_model_id": "mm-1"})
        finally:
            await client.close()

    def test_whole_lines_bind_continuation_to_version_and_include_all_wrapping_cost(self):
        page = {"id": "kp-1", "name": "Native", "body": "".join(f"完整原文第 {i} 行，保留约束条件。\n" for i in range(300)),
                "version": "a" * 64, "last_refreshed_at": None}
        limits = {"tokens": 256, "chars": 1000, "bytes_limit": 1500, "tokenizer": "o200k_base"}
        result = reading.page_excerpt(page, **limits)
        self.assertEqual(result["content"], "".join(page["body"].splitlines(keepends=True)[:result["end_line"]]))
        self.assertTrue(reading.fits({"kind": "page", "external_data": True, **result}, **limits))
        continued = reading.page_excerpt(page, start_line=result["next_line"], version=result["version"], **limits)
        self.assertEqual(continued["start_line"], result["end_line"] + 1)
        with self.assertRaisesRegex(HindsightError, "invalid_read_version"):
            reading.page_excerpt(page, start_line=2, **limits)
        with self.assertRaisesRegex(HindsightError, "read_version_changed"):
            reading.page_excerpt({**page, "version": "b" * 64}, start_line=2, version=result["version"], **limits)
        with self.assertRaisesRegex(HindsightError, "read_line_too_large"):
            reading.page_excerpt({**page, "body": "one oversized complete line " * 1000}, **limits)

    def test_native_source_ids_attribution_and_missing_evidence_are_not_invented(self):
        record = reading.recall_records([{
            "id": "observation", "text": "Native observation", "type": "observation",
            "source_fact_ids": ["f-1"], "document_id": "doc", "chunk_id": "chunk",
            "metadata": {"role": "assistant", "delivery": "unknown", "api_key": "private"},
        }], "bank")[0]
        self.assertEqual(record["source_fact_ids"], ["f-1"])
        self.assertEqual(record["metadata"], {"role": "assistant", "delivery": "unknown"})
        self.assertNotIn("private", canonical(record))
        self.assertFalse(reading.native_sources({"based_on": None})["available"])
        with self.assertRaisesRegex(HindsightError, "invalid_recall_response"):
            reading.recall_records([{**record, "bank_id": "another-bank"}], "bank")


class RunContext:
    pass


class ToolBoundaries(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = runtime.RuntimeDiagnostics.asyncTearDown
    turn = runtime.RuntimeDiagnostics.turn
    service = hooks.BackgroundHooks.service
    scope = hooks.BackgroundHooks.scope

    async def asyncSetUp(self):
        await runtime.RuntimeDiagnostics.asyncSetUp(self)
        self.tools = self.plugin.read_tools
        self.provider = SimpleNamespace(provider_config={"id": "fixture", "modalities": ["tool_use"]}, get_model=lambda: "fixture")
        self.context.get_using_provider_async = AsyncMock(return_value=self.provider)
        self.tools.owned = {name: SimpleNamespace(name=name, active=True, handler_module_path="bridge.main") for name in NAMES}
        self.tools.manager = SimpleNamespace(get_func=lambda name: self.tools.owned.get(name), remove_func=lambda name: None)

    async def ready(self):
        event, request = self.turn()
        scope = self.scope()
        save_options(self.plugin.outbox, scope, revision=0, mode="tools")
        request.func_tool = SimpleNamespace(tools=list(self.tools.owned.values()))
        original = self.tools.filter_request(event, request)
        self.assertEqual(request.func_tool.tools, [])
        await self.tools.prepare(event, request, self.incoming, scope, original)
        self.assertEqual(len(request.func_tool.tools), len(NAMES))
        context = RunContext()
        context.context = SimpleNamespace(context=self.context, event=event)
        return event, context

    async def test_parallel_calls_share_one_ledger_and_only_filter_owned_entries(self):
        event, context = await self.ready()
        foreign = SimpleNamespace(name="foreign", active=True, handler_module_path="other.main")
        original = SimpleNamespace(tools=[*self.tools.owned.values(), foreign])
        request = SimpleNamespace(func_tool=original)
        self.tools.filter_request(object(), request)
        self.assertEqual(request.func_tool.tools, [foreign])
        self.assertEqual(len(original.tools), len(NAMES) + 1)
        with patch.object(self.tools, "read", AsyncMock(return_value={"kind": "recall", "external_data": True, "items": []})) as read:
            results = await asyncio.gather(*(self.tools.execute(context, "recall", {"query": "q"}) for _ in range(9)))
        self.assertEqual(read.await_count, 4)
        self.assertEqual(self.tools.turns[event].calls, 4)
        self.assertTrue(any("tool_budget_exhausted" in (item or "") for item in results))
        self.assertLessEqual(self.tools.turns[event].output_tokens, 2 * self.tools.turns[event].tokens)

    async def test_tool_result_drops_after_model_change_and_arguments_cannot_select_bank(self):
        event, context = await self.ready()
        value = await self.tools.execute(context, "recall", {"query": "q", "bank": "other-bank"})
        self.assertIn("invalid_tool_arguments", value)

        async def change(*args):
            self.provider.provider_config["model"] = "new-model"
            return {"content": "private-late-result"}
        with patch.object(self.tools, "read", AsyncMock(side_effect=change)):
            value = await self.tools.execute(context, "recall", {"query": "q"})
        self.assertNotIn("private-late-result", value)
        self.assertIn("reading_changed", value)
        self.assertFalse(any("other-bank" in str(request.url) for request in self.requests))

    async def test_waiting_for_shared_ledger_uses_the_same_call_deadline(self):
        self.plugin.config["recall_timeout"] = 0.2
        _, context = await self.ready()
        started = time.monotonic()
        async def delayed(*args):
            await asyncio.sleep(0.4)
            return {"kind": "recall", "items": []}
        with patch.object(self.tools, "read", AsyncMock(side_effect=delayed)):
            results = await asyncio.gather(*(self.tools.execute(context, "recall", {"query": "q"}) for _ in range(3)))
        self.assertLess(time.monotonic() - started, 0.35)
        self.assertTrue(all("tool_read_timeout" in value for value in results))

    async def test_closing_cancels_owned_read_without_cancelling_host_task(self):
        _, context = await self.ready()
        entered = asyncio.Event()

        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(self.tools, "read", AsyncMock(side_effect=blocked)):
            host = asyncio.create_task(self.tools.execute(context, "recall", {"query": "q"}))
            await entered.wait()
            await self.tools.close()
            self.assertEqual(json.loads(await host)["error"], "plugin_stopping")
            self.assertFalse(host.cancelled())
        self.assertEqual(self.plugin.hooks, 0)

    async def test_capability_discovery_failure_does_not_disable_automatic_recall(self):
        self.context.get_using_provider_async.side_effect = RuntimeError("provider unavailable")
        event, request = self.turn()
        self.results = [{"id": "fact", "text": "Automatic recall survives.", "type": "world"}]
        await self.plugin.add_memory(event, request)
        self.assertIn("Automatic recall survives.", request.extra_user_content_parts[0].text)
        self.assertEqual(self.plugin.outbox.db.execute("SELECT state FROM outbox").fetchone()[0], "ready")

    async def test_forget_resets_options_with_revision_tombstone_and_invalidates_inflight_results(self):
        event, context = await self.ready()
        before = read_options(self.plugin.outbox, self.scope())

        async def forget(*args):
            self.plugin.outbox.forget(self.scope()["scope_id"])
            return {"content": "retired-bank-result"}
        with patch.object(self.tools, "read", AsyncMock(side_effect=forget)):
            result = await self.tools.execute(context, "recall", {"query": "q"})
        self.assertNotIn("retired-bank-result", result)
        after = read_options(self.plugin.outbox, self.scope())
        self.assertEqual(after["mode"], "automatic")
        self.assertGreater(after["revision"], before["revision"])
        self.assertTrue(after["invalidated"])
        with self.assertRaisesRegex(QueueError, "reading_changed"):
            save_options(self.plugin.outbox, self.scope(), revision=before["revision"], mode="tools")
