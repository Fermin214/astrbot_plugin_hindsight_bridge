"""Boundaries that are difficult to force deterministically through the Agent."""

import asyncio
from dataclasses import replace
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
from plugins.astrbot_plugin_hindsight_bridge.core.background import compose_backgrounds
from plugins.astrbot_plugin_hindsight_bridge.core.client import BACKGROUND_MAX_BYTES, HindsightClient, HindsightError
from plugins.astrbot_plugin_hindsight_bridge.core.control import QueueError
from plugins.astrbot_plugin_hindsight_bridge.core.identity import canonical
from plugins.astrbot_plugin_hindsight_bridge.core.reference import reference_options, save_reference_options


class ReadContracts(unittest.IsolatedAsyncioTestCase):
    async def test_negative_authentication_check_runs_once_and_real_key_failures_keep_validation(self):
        credentials = []
        status = 200

        async def service(request):
            credential = request.headers.get("Authorization")
            credentials.append(credential)
            return httpx.Response(status if credential in {"Bearer key", "Bearer changed"} else 401, json={"banks": []})

        client = HindsightClient("http://memory.test", "key", transport=httpx.MockTransport(service))
        try:
            await client.check_connection(force=True)
            fingerprint = client.connection_fingerprint(client.url, client.api_key)
            self.assertEqual(client.auth_verified, fingerprint)
            await client.check_connection(force=True)
            self.assertEqual(sum(value.startswith("Bearer invalid-") for value in credentials), 1)
            status = 401
            with self.assertRaisesRegex(HindsightError, "http_401"):
                await client.check_connection(force=True)
            self.assertEqual(client.auth_verified, fingerprint)
            status = 200
            await client.check_connection(force=True)
            self.assertEqual(sum(value.startswith("Bearer invalid-") for value in credentials), 1)
            client.api_key = "changed"
            await client.check_connection()
            self.assertEqual(sum(value.startswith("Bearer invalid-") for value in credentials), 2)
            restarted = HindsightClient(client.url, "changed", transport=httpx.MockTransport(service), auth_verified=True)
            try:
                await restarted.check_connection(force=True)
                self.assertEqual(sum(value.startswith("Bearer invalid-") for value in credentials), 2)
            finally:
                await restarted.close()
        finally:
            await client.close()

    async def test_unprotected_service_is_rejected_without_marking_credentials_verified(self):
        calls = []

        async def service(request):
            calls.append(request)
            return httpx.Response(200, json={"banks": []})

        client = HindsightClient("http://memory.test", "key", transport=httpx.MockTransport(service))
        try:
            for _ in range(2):
                with self.assertRaisesRegex(HindsightError, "server_auth_not_enforced"):
                    await client.check_connection(force=True)
            self.assertEqual(len(calls), 2)
            self.assertFalse(client.auth_verified)
        finally:
            await client.close()

    async def test_unknown_operation_states_and_extra_metadata_are_retained_for_followup(self):
        parent = {"operation_id": "parent", "bank_id": "bank", "status": "waiting_for_embeddings",
                  "child_operations": [{"operation_id": "child", "status": "checkpoint_pending", "new_metadata": {"key": ["future"]}}],
                  "new_metadata": {"key": ["future"]}}

        async def service(request):
            if request.url.path.endswith("/operations"):
                return httpx.Response(200, json={"operations": [parent], "total": 1, "extra": ["future"]})
            return httpx.Response(200, json=parent)

        client = HindsightClient("http://memory.test", "key", transport=httpx.MockTransport(service), auth_verified=True)
        client.auth_until = time.monotonic() + 60
        try:
            operation = await client.operation("bank", "parent")
            listed, = await client.list_operations("bank")
            self.assertEqual(operation.status, "waiting_for_embeddings")
            self.assertEqual(operation.children, (("child", "checkpoint_pending"),))
            self.assertEqual(operation, listed)
            parent["status"] = "completed"
            self.assertEqual((await client.operation("bank", "parent")).children[0][1], "checkpoint_pending")
            parent["child_operations"] = {}
            with self.assertRaisesRegex(HindsightError, "invalid_operation_children"):
                await client.operation("bank", "parent")
        finally:
            await client.close()

    def test_request_marker_is_inside_the_shared_background_budget(self):
        prefix = "<hb-read-request:" + "a" * 24 + ">\n"
        rows = [{"text": "complete fact " * 20}]
        block, _, _ = compose_backgrounds([], rows, tokens=100, chars=170, tokenizer="o200k_base", prefix=prefix)
        self.assertLessEqual(len(prefix + block), 170)
        self.assertLessEqual(reading.token_count(prefix + block, "o200k_base"), 100)

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

    def test_filter_keeps_mixed_and_incomplete_native_attribution(self):
        user = {"id": "user", "type": "world", "text": "request", "document_id": "uw1-0-" + "a" * 64}
        assistant = {"id": "assistant", "type": "experience", "text": "promise", "document_id": "aw1-12-" + "b" * 64}
        custom = {"id": "custom", "type": "world", "text": "custom document", "document_id": "aw1-12-import"}
        observations = [
            {"id": name, "type": "observation", "text": name, "source_fact_ids": ids}
            for name, ids in (
                ("known", ["assistant"]), ("both", ["assistant", "user"]),
                ("incomplete", ["assistant", "absent"]), ("imported", ["assistant", "custom"]), ("unlinked", []),
            )
        ]
        records = reading.attributed_records(
            [assistant, custom, *observations], "bank", {row["id"]: row for row in (user, assistant)},
        )
        selected, excluded = reading.chat_records(records, "bank", {
            "types": ["world", "experience", "observation"], "exclude_assistant": True,
        })
        self.assertEqual(excluded, 2)
        self.assertEqual([(row["id"], row["source_kind"]) for row in selected], [
            ("custom", "external"), ("both", "mixed"), ("incomplete", "unknown"),
            ("imported", "unknown"), ("unlinked", "unknown"),
        ])

    def test_external_direct_records_do_not_supply_missing_observation_role_evidence(self):
        rows = [
            {"id": "legacy", "type": "world", "text": "old", "document_id": "legacy-0104"},
            {"id": "user", "type": "experience", "text": "user", "document_id": "uw1-0-" + "a" * 64},
            {"id": "assistant", "type": "experience", "text": "assistant", "document_id": "aw1-12-" + "b" * 64},
            {"id": "mixed_incomplete", "type": "observation", "text": "mixed", "source_fact_ids": ["user", "assistant", "legacy"]},
        ]
        records = reading.attributed_records(rows, "bank", None, strict=True)
        self.assertEqual([record["source_kind"] for record in records], ["external", "user", "assistant", "unknown"])

    async def test_directory_metadata_is_connection_bank_mapping_bound_and_snapshot_values_are_copied(self):
        tree = {"roots": [{"id": "kp-1", "kind": "page", "name": "First", "mental_model_id": "mm-1", "is_stale": False, "children": []}]}

        async def service(request):
            return httpx.Response(200, json=tree)

        client = HindsightClient("http://memory.test", "key", transport=httpx.MockTransport(service))
        try:
            self.assertEqual(client.cached_page_metadata("bank", "kp-1", "mm-1"), {})
            await client.knowledge_pages("bank")
            frozen = client.cached_page_metadata("bank", "kp-1", "mm-1")
            self.assertEqual(frozen, {"name": "First", "path": "First", "is_stale": False})
            self.assertEqual(client.cached_page_metadata("other-bank", "kp-1", "mm-1"), {})
            self.assertEqual(client.cached_page_metadata("bank", "kp-1", "mm-2"), {})
            client.api_key = "changed"
            self.assertEqual(client.cached_page_metadata("bank", "kp-1", "mm-1"), {})
            client.api_key = "key"
            tree["roots"][0].update(name="Renamed", is_stale=True)
            await client.knowledge_pages("bank")
            self.assertEqual(frozen["name"], "First")
            self.assertFalse(frozen["is_stale"])
            self.assertEqual(client.cached_page_metadata("bank", "kp-1", "mm-1")["name"], "Renamed")
            with patch.object(time, "monotonic", return_value=time.monotonic() + 31):
                self.assertEqual(client.cached_page_metadata("bank", "kp-1", "mm-1"), {})
        finally:
            await client.close()


class RunContext:
    pass


class ToolRequest:
    pass


class ToolSet:
    def __init__(self, tools):
        self.tools = tools


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
        self.tools.owned = {name: SimpleNamespace(name=name, description=name, active=True, handler_module_path="bridge.main") for name in NAMES}
        self.tools.manager = SimpleNamespace(get_func=lambda name: self.tools.owned.get(name), remove_func=lambda name: None)

    async def ready(self, *, readonly=False):
        event, request = self.turn()
        request = self.request = ToolRequest()
        request.model = None
        request.prompt = "source"
        scope = self.scope()
        if readonly:
            self.incoming = replace(self.incoming, capture_allowed=False)
            self.plugin.incoming[event] = self.incoming
            save_reference_options(self.plugin.outbox, scope, **{
                **reference_options(self.plugin.outbox, scope), "recall_on_quoted_or_link": True,
            })
        save_options(self.plugin.outbox, scope, revision=0, mode="tools")
        request.func_tool = ToolSet(list(self.tools.owned.values()))
        original = self.tools.filter_request(event, request)
        self.assertEqual(request.func_tool.tools, [])
        await self.tools.prepare(event, request, self.incoming, scope, original)
        self.assertEqual(len(request.func_tool.tools), len(NAMES))
        context = RunContext()
        context.context = SimpleNamespace(context=self.context, event=event)
        context.messages = [SimpleNamespace(role="user", content=[SimpleNamespace(text="source", _no_save=False), SimpleNamespace(
            text=self.tools.request_prefix(event) + "notice", _no_save=True,
        )])]
        await self.plugin.bind_memory_tools(event, context)
        return event, context

    async def test_readonly_tools_cannot_read_after_bot_identity_changes(self):
        event, context = await self.ready(readonly=True)
        queue = self.plugin.outbox
        self.assertFalse(self.incoming.capture_allowed)
        queue.access.observe_bot("qq", "aiocqhttp", "another-bot")
        with patch.object(self.tools, "read", AsyncMock()) as read:
            self.assertIn("reading_changed", await self.tools.execute(context, "recall", {"query": "q"}))
        read.assert_not_awaited()
        self.assertEqual(queue.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)

    async def test_parallel_calls_share_one_ledger_and_only_filter_owned_entries(self):
        event, context = await self.ready()
        foreign = SimpleNamespace(name="foreign", active=True, handler_module_path="other.main")
        original = SimpleNamespace(tools=[*self.tools.owned.values(), foreign])
        request = SimpleNamespace(func_tool=original)
        self.tools.filter_request(object(), request)
        self.assertEqual(request.func_tool.tools, [foreign])
        self.assertEqual(len(original.tools), len(NAMES) + 1)
        with patch.object(self.tools, "read", AsyncMock(return_value={"kind": "recall", "external_data": True, "items": []})) as read:
            turn = self.tools.tool_turns[event]
            history = self.plugin.outbox.references
            turn.snapshot_id = history.start(turn.guard.scope, "chat", "message", {"kind": "request"})
            results = await asyncio.gather(*(self.tools.execute(context, "recall", {"query": "q"}) for _ in range(9)))
        self.assertEqual(read.await_count, 4)
        self.assertEqual(turn.calls, 4)
        self.assertTrue(any("tool_budget_exhausted" in (item or "") for item in results))
        self.assertEqual(json.loads(results[-1]), {"error": "tool_context_unavailable"})
        foreign = RunContext()
        foreign.context = context.context
        self.assertEqual(await self.tools.execute(foreign, "recall", {"query": "q"}), results[-1])
        deliveries = history.detail(turn.guard.scope, turn.snapshot_id)["deliveries"][1:]
        self.assertEqual([delivery["text"] for delivery in deliveries], results)
        self.assertEqual(deliveries[-1]["status"], "error")
        paused = self.plugin.outbox.set_mode(turn.guard.scope["scope_id"], "paused")
        self.assertEqual(await self.tools.execute(context, "recall", {"query": "q"}), results[-1])
        self.assertEqual(history.detail(paused, turn.snapshot_id)["deliveries"][1:], deliveries)
        self.assertLessEqual(turn.output_tokens, 2 * turn.tokens)
        self.assertEqual(self.request.func_tool.tools, [])
        self.assertEqual(turn.expires, 0)
        self.tools.finish(event, context)
        self.assertNotIn(event, self.tools.tool_turns)

    async def test_overlapping_reads_share_final_ids_and_skip_past_duplicates(self):
        scope = self.scope()
        options = reference_options(self.plugin.outbox, scope)
        save_reference_options(self.plugin.outbox, scope, **{
            **options, "detail_limit": 3, "types": ["world"], "recall_budget": "high",
        })
        event, context = await self.ready()
        turn = self.tools.tool_turns[event]
        turn.snapshot_id = "test-record"

        def rows(bank, query, *args, **kwargs):
            self.assertEqual(kwargs["types"], ["world"])
            self.assertEqual(kwargs["budget"], "high")
            return [{"id": identity, "text": identity, "type": "world"} for identity in query]

        with (
            patch.object(self.plugin.client, "recall", AsyncMock(side_effect=rows)) as recall,
            patch.object(self.plugin, "record_tool_delivery") as write,
        ):
            raw = await asyncio.gather(*(self.tools.execute(context, "recall", {"query": query}) for query in (
                "AB", "B" * 16 + "C",
            )))
            self.assertEqual([[row["id"] for row in json.loads(text)["items"]] for text in raw], [["A", "B"], ["C"]])
            limited = await self.tools.execute(context, "recall", {"query": "D"})
            self.assertEqual(json.loads(limited)["status"], "limit")
            self.assertEqual(recall.await_count, 2)
            self.assertEqual(turn.delivered_ids, {"A", "B", "C"})
            self.assertEqual([call.args[2] for call in write.call_args_list], [*raw, limited])

    async def test_only_agent_begin_binds_context_and_agent_done_retires_unused_turn(self):
        event, context = await self.ready()
        foreign = RunContext()
        foreign.context = context.context
        foreign.messages = [SimpleNamespace(role="user", content=[SimpleNamespace(text="independent task", _no_save=False)])]
        turn = self.tools.tool_turns[event]
        turn.context_ref = None
        with patch.object(self.tools, "read", AsyncMock()) as read:
            self.tools.begin(event, foreign)
            self.assertIsNone(turn.context_ref)
            self.assertFalse(self.tools.finish(event, foreign))
            foreign.messages[0].content.append(context.messages[0].content[-1])
            self.tools.begin(event, foreign)
            self.assertIsNone(turn.context_ref)
            self.assertIn("tool_context_unavailable", await self.tools.execute(foreign, "recall", {"query": "q"}))
            self.tools.begin(event, context)
            self.assertIn("tool_context_unavailable", await self.tools.execute(foreign, "recall", {"query": "q"}))
            self.tools.finish(event, foreign)
            self.assertIn(event, self.tools.tool_turns)
            self.tools.finish(event, context)
            self.assertNotIn(event, self.tools.tool_turns)
            self.assertIn("tool_context_unavailable", await self.tools.execute(context, "recall", {"query": "q"}))
            read.assert_not_awaited()

    async def test_live_request_model_change_and_unverifiable_fallback_disable_reading(self):
        event, context = await self.ready()
        with patch.object(self.tools, "read", AsyncMock()) as read:
            self.request.model = "later-hook-model"
            self.assertIn("reading_changed", await self.tools.execute(context, "recall", {"query": "q"}))
            self.assertFalse(self.tools.public(self.scope())["observed_available"])
            self.request.model = None
            self.context.get_config = lambda *args: {"agent_runner": {
                "runner_type": "local", "config": {"model": {"fallback_provider_ids": ["other"]}},
            }}
            self.context.get_provider_by_id = lambda identifier: None
            self.assertFalse((await self.tools.provider_state(event.unified_msg_origin))[1])
            self.assertFalse(await self.tools.availability(self.scope()))
            self.assertIn("tool_context_unavailable", await self.tools.execute(context, "recall", {"query": "q"}))
            read.assert_not_awaited()
        self.request.func_tool.tools = list(self.tools.owned.values())
        self.tools.finish(event, context)
        await self.tools.prepare(event, self.request, self.incoming, self.scope(), self.request.func_tool)
        self.assertFalse(self.tools.public(self.scope())["observed_available"])
        self.assertNotIn(event, self.tools.tool_turns)

    async def test_all_candidates_must_explicitly_support_tools(self):
        fallback = SimpleNamespace(provider_config={"id": "fallback", "modalities": ["tool_use"]}, get_model=lambda: "backup")
        self.context.get_provider_by_id = lambda identifier: fallback if identifier == "backup" else None
        self.context.get_config = lambda *args: {"agent_runner": {
            "runner_type": "local", "config": {"model": {"fallback_provider_ids": ["backup"]}},
        }}
        event, context = await self.ready()
        self.plugin.prepare_sources = AsyncMock()
        self.plugin.client.ensure_auth = AsyncMock()
        self.assertTrue((await self.tools.provider_state(event.unified_msg_origin))[1])
        with patch.object(self.tools, "read", AsyncMock(return_value={"kind": "recall", "items": []})):
            self.assertIn("items", json.loads(await self.tools.execute(context, "recall", {"query": "q"})))
        for modalities in (None, [], ["text"]):
            fallback.provider_config["modalities"] = modalities
            self.assertFalse((await self.tools.provider_state(event.unified_msg_origin))[1])
        fallback.provider_config["modalities"] = ["tool_use"]
        self.provider.provider_config.pop("modalities")
        self.assertFalse((await self.tools.provider_state(event.unified_msg_origin))[1])

    async def test_native_light_schemas_preserve_request_ownership_and_retire_cached_source_tools(self):
        event, context = await self.ready()
        turn = self.tools.tool_turns[event]
        turn.context_ref = None
        source = self.request.func_tool
        foreign = SimpleNamespace(name="foreign", description="foreign", active=True, handler_module_path="other.main")
        source.tools.append(foreign)
        self.request.func_tool = ToolSet([SimpleNamespace(
            name=tool.name, description=tool.description, parameters={"type": "object", "properties": {}},
            handler=None, handler_module_path=None, active=True,
        ) for tool in source.tools])
        self.tools.begin(event, context)
        self.assertIs(turn.context_ref(), context)
        with patch.object(self.tools, "read", AsyncMock(return_value={"kind": "recall", "items": []})) as read:
            results = [await self.tools.execute(context, "recall", {"query": "q"}) for _ in range(5)]
        self.assertEqual(read.await_count, 4)
        self.assertIn("tool_budget_exhausted", results[-1])
        self.assertEqual([tool.name for tool in self.request.func_tool.tools], ["foreign"])
        self.assertEqual(source.tools, [foreign])
        self.assertTrue(self.tools.active())

    async def test_output_exhaustion_returns_a_notice_and_never_ends_host_chat_with_none(self):
        event, context = await self.ready()
        turn = self.tools.tool_turns[event]
        turn.tokens = 256
        turn.snapshot_id = "test-record"
        with patch.object(self.tools, "read", AsyncMock(side_effect=[
            {"kind": "recall", "items": [{"id": identity, "text": "word " * 235}]} for identity in ("first", "second")
        ])), patch.object(self.plugin, "record_tool_delivery") as write:
            results = [await self.tools.execute(context, "recall", {"query": "q"}) for _ in range(2)]
        self.assertTrue(all(isinstance(value, str) and value for value in results))
        self.assertIn("tool_budget_exhausted", results[-1])
        self.assertLessEqual(turn.output_tokens, 2 * turn.tokens)
        self.assertLessEqual(turn.output_bytes, 16 * 1024)
        self.assertEqual(turn.delivered_ids, {"first"})
        self.assertEqual(write.call_args.args[2:], (results[-1], "budget"))
        self.assertEqual(self.request.func_tool.tools, [])

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
