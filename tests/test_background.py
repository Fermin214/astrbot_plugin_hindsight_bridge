"""Native page contracts, durable references, and shared context limits."""

import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

import httpx

from plugins.astrbot_plugin_hindsight_bridge.core.background import (
    ReadGuard, compose_background, reference, reference_key, save_reference,
)
from plugins.astrbot_plugin_hindsight_bridge.core.client import (
    BACKGROUND_MAX_BYTES, HindsightClient, HindsightError, native_page_id,
)
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming
from plugins.astrbot_plugin_hindsight_bridge.core.materials import token_count
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox, QueueError


def native_model(bank="bank", content="A short native background."):
    return {
        "id": "mm-1", "bank_id": bank, "is_knowledge_page": True, "content": content,
        "is_stale": None, "last_refreshed_at": "2026-11-04T00:00:00+00:00",
        "last_memory_seen_at": None, "last_refresh_failed_at": None,
        "reflect_response": {"private": "must not be projected"},
    }


def native_tree():
    return {"roots": [{
        "id": "kf-1", "kind": "folder", "name": "Folder", "mental_model_id": None,
        "children": [{
            "id": "kp-1", "kind": "page", "name": "Background", "mental_model_id": "mm-1",
            "is_stale": False, "children": [],
        }],
    }]}


class NativeReads(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.values = {
            "/v1/default/banks/bank/knowledge-base/tree": native_tree(),
            "/v1/default/banks/bank/knowledge-base/pages/kp-1": {
                "id": "kp-1", "name": "Background", "body": "A short native background.",
                "markdown": "frontmatter and placeholder must not become content",
            },
            "/v1/default/banks/bank/mental-models/mm-1": native_model(),
            "/v1/default/banks/bank/config": {"bank_id": "bank"},
        }

        async def service(request):
            self.calls.append(request)
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["Authorization"], "Bearer key")
            value = self.values.get(request.url.path)
            if isinstance(value, httpx.Response):
                return value
            return httpx.Response(200 if value is not None else 404, json=value or {})

        self.client = HindsightClient("http://memory.test", "key", transport=httpx.MockTransport(service))

    async def asyncTearDown(self):
        await self.client.close()

    async def test_tree_mapping_body_and_light_content_are_projected_separately(self):
        pages = await self.client.knowledge_pages("bank")
        self.assertEqual(pages[0]["path"], "Folder / Background")
        self.assertFalse(pages[0]["index_stale"])
        page = await self.client.knowledge_page("bank", "kp-1")
        model = await self.client.background_page("bank", "mm-1")
        self.assertEqual(page["body"], model["content"])
        self.assertEqual(model["freshness"], "unknown")
        self.assertNotIn("reflect_response", model)
        self.assertNotIn("is_stale", model)
        self.assertEqual(self.calls[-1].url.params["detail"], "content")
        self.assertEqual(len(self.calls), 3)

    async def test_empty_page_never_uses_rendered_placeholder(self):
        self.values["/v1/default/banks/bank/knowledge-base/pages/kp-1"]["body"] = None
        page = await self.client.knowledge_page("bank", "kp-1")
        self.assertEqual(page["body"], "")

    async def test_404_page_is_distinct_from_missing_bank_and_never_creates(self):
        del self.values["/v1/default/banks/bank/mental-models/mm-1"]
        with self.assertRaisesRegex(HindsightError, "background_missing"):
            await self.client.background_page("bank", "mm-1")
        del self.values["/v1/default/banks/bank/config"]
        with self.assertRaisesRegex(HindsightError, "bank_missing"):
            await self.client.background_page("bank", "mm-1")
        self.assertTrue(all(call.method == "GET" for call in self.calls))

    async def test_identifiers_cannot_supply_paths_urls_or_unbounded_values(self):
        for value in (None, "", "..", ".", "x/y", "x\\y", "%2f", "https://x", "x?detail=full", "a" * 257):
            with self.subTest(value=value), self.assertRaises(HindsightError):
                native_page_id(value)
        self.assertEqual(native_page_id("mm-123_abc"), "mm-123_abc")

    async def test_duplicate_models_unknown_nodes_and_nonboolean_stale_are_rejected(self):
        original = native_tree()
        variants = []
        repeated = copy.deepcopy(original)
        repeated["roots"][0]["children"].append(copy.deepcopy(repeated["roots"][0]["children"][0]))
        repeated["roots"][0]["children"][1]["id"] = "kp-2"
        variants.append(repeated)
        for field, value in (("kind", "unknown"), ("kind", []), ("is_stale", 0), ("children", {})):
            variant = copy.deepcopy(original)
            variant["roots"][0]["children"][0][field] = value
            variants.append(variant)
        for variant in variants:
            self.values["/v1/default/banks/bank/knowledge-base/tree"] = variant
            with self.assertRaisesRegex(HindsightError, "invalid_background_response"):
                await self.client.knowledge_pages("bank")

    async def test_tree_depth_and_node_count_have_hard_limits(self):
        node = native_tree()["roots"][0]
        for n in range(12):
            node = {"id": f"kf-{n + 2}", "kind": "folder", "name": "Folder", "children": [node]}
        self.values["/v1/default/banks/bank/knowledge-base/tree"] = {"roots": [node]}
        with self.assertRaisesRegex(HindsightError, "background_index_too_large"):
            await self.client.knowledge_pages("bank")
        self.values["/v1/default/banks/bank/knowledge-base/tree"] = {"roots": [
            {"id": f"kf-{n}", "kind": "folder", "name": "Folder", "children": []} for n in range(501)
        ]}
        with self.assertRaisesRegex(HindsightError, "background_index_too_large"):
            await self.client.knowledge_pages("bank")

    async def test_model_bank_identity_and_native_times_are_verified(self):
        for key, value in (
            ("bank_id", "other-bank"), ("id", "other-id"), ("is_knowledge_page", False),
            ("content", []), ("last_refreshed_at", "2026-11-04T00:00:00"),
            ("last_refresh_failed_at", "invalid"),
        ):
            self.values["/v1/default/banks/bank/mental-models/mm-1"] = {**native_model(), key: value}
            with self.subTest(key=key), self.assertRaisesRegex(HindsightError, "invalid_background_response"):
                await self.client.background_page("bank", "mm-1")

    async def test_stream_limit_interrupts_body_before_full_download_and_closes(self):
        class Stream(httpx.AsyncByteStream):
            consumed = 0
            closed = False

            async def __aiter__(self):
                for _ in range(100):
                    self.consumed += 8192
                    yield b"x" * 8192

            async def aclose(self):
                self.closed = True

        stream = Stream()
        self.values["/v1/default/banks/bank"] = httpx.Response(200, stream=stream)
        with self.assertRaisesRegex(HindsightError, "background_response_too_large"):
            await self.client.read_bounded("/v1/default/banks/bank")
        self.assertLessEqual(stream.consumed, BACKGROUND_MAX_BYTES + 16384)
        self.assertTrue(stream.closed)

    async def test_invalid_json_and_redirect_do_not_leak_upstream_body(self):
        for response, code in (
            (httpx.Response(200, content=b"private-key not JSON"), "invalid_background_response"),
            (httpx.Response(302, headers={"Location": "http://other.test"}), "http_302"),
        ):
            self.values["/v1/default/banks/bank"] = response
            with self.assertRaisesRegex(HindsightError, code) as caught:
                await self.client.read_bounded("/v1/default/banks/bank")
            self.assertNotIn("private-key", str(caught.exception))


class References(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.queue = Outbox(self.path / "runtime", "service", "http://memory.test", credential_fingerprint="key")
        self.incoming = Incoming("aiocqhttp", "qq", "bot", "user", "one", time.time() + 2, "hello")
        self.queue.sync_permissions([("qq", "user")])
        self.queue.activate(self.incoming, self.queue.access.authorization("qq", "user")["revision"])
        self.scope = self.queue.scope(self.incoming)
        self.plugin = SimpleNamespace(
            outbox=self.queue, client=object(), stopping=False, permission_revision=1,
            config={"service_id": "service", "api_url": "http://memory.test", "api_key": "key"},
            refresh_permissions=lambda: None, allowed=lambda event: True,
        )

    def tearDown(self):
        self.queue.close()
        self.tmp.cleanup()

    def select(self, enabled=True, revision=0):
        return save_reference(self.queue, self.scope, revision=revision, page_id="kp-1", model_id="mm-1", enabled=enabled)

    def test_default_off_and_save_only_reference_metadata(self):
        self.assertEqual(reference(self.queue, self.scope)["enabled"], False)
        self.assertEqual(self.select()["revision"], 1)
        stored = self.queue.control.setting(reference_key(self.queue.connection_id, self.scope["owner_id"]))
        self.assertNotIn("content", stored)
        self.assertNotIn("api_key", stored)
        with self.assertRaisesRegex(QueueError, "background_changed"):
            self.select()

    def test_close_preserves_official_reference_but_invalidates_inflight_read(self):
        self.select()
        guard = ReadGuard.freeze(self.plugin, self.scope)
        self.assertTrue(guard.valid(self.plugin))
        saved = self.select(False, 1)
        self.assertEqual(saved["page_id"], "kp-1")
        self.assertFalse(saved["enabled"])
        self.assertFalse(guard.valid(self.plugin))

    def test_revoke_and_reauthorize_does_not_revive_background(self):
        self.select()
        guard = ReadGuard.freeze(self.plugin, self.scope)
        self.queue.sync_permissions([])
        self.assertFalse(guard.valid(self.plugin))
        self.queue.sync_permissions([("qq", "user")])
        self.queue.activate(self.incoming, self.queue.access.authorization("qq", "user")["revision"])
        self.scope = self.queue.scope(self.incoming)
        saved = reference(self.queue, self.scope)
        self.assertEqual(saved["revision"], 1)
        self.assertFalse(saved["enabled"])
        self.assertEqual(saved["page_id"], "")

    def test_paused_manual_read_differs_from_automatic_and_maintenance_blocks_both(self):
        self.select()
        self.queue.set_mode(self.scope["scope_id"], "paused")
        self.scope = self.queue.scope(self.incoming)
        self.assertFalse(ReadGuard.freeze(self.plugin, self.scope).valid(self.plugin))
        manual = ReadGuard.freeze(self.plugin, self.scope, manual=True)
        self.assertTrue(manual.valid(self.plugin))
        self.queue.control.begin_maintenance(self.queue.connection_id, self.scope["owner_id"])
        self.queue.apply_control()
        self.assertFalse(manual.valid(self.plugin))
        self.scope = self.queue.scope(self.incoming)
        self.assertFalse(ReadGuard.freeze(self.plugin, self.scope, manual=True).valid(self.plugin))
        self.select(False, 1)

    def test_forget_clears_selection_with_durable_revision_tombstone(self):
        self.select()
        guard = ReadGuard.freeze(self.plugin, self.scope)
        self.queue.forget(self.scope["scope_id"])
        self.scope = self.queue.scope(self.incoming)
        self.assertFalse(guard.valid(self.plugin))
        saved = reference(self.queue, self.scope)
        self.assertEqual(saved, {"revision": 2, "enabled": False, "page_id": "", "mental_model_id": ""})
        self.queue.close()
        self.queue = Outbox(self.path / "runtime", "service", "http://memory.test", credential_fingerprint="key")
        self.assertEqual(reference(self.queue, self.queue.scope_owner(self.scope["scope_id"])), saved)

    def test_connection_config_permission_and_instance_changes_drop_old_reads(self):
        self.select()
        for change in (
            lambda: self.plugin.config.update(api_key="other"),
            lambda: self.plugin.config.update(api_url="http://other.test"),
            lambda: self.plugin.config.update(context_max_chars=256),
            lambda: self.plugin.config.update(recall_max_tokens=256),
            lambda: self.plugin.config.update(recall_timeout=0.2),
            lambda: setattr(self.plugin, "permission_revision", 2),
            lambda: setattr(self.plugin, "client", object()),
            lambda: setattr(self.plugin, "stopping", True),
        ):
            guard = ReadGuard.freeze(self.plugin, self.scope)
            change()
            self.assertFalse(guard.valid(self.plugin))
            self.plugin.stopping = False

    def test_unknown_selection_format_fails_closed(self):
        key = reference_key(self.queue.connection_id, self.scope["owner_id"])
        with self.queue.control.transaction():
            self.queue.control.db.execute("INSERT INTO settings VALUES (?,?)", (key, '{"format":2}'))
        with self.assertRaisesRegex(QueueError, "background_settings_invalid"):
            reference(self.queue, self.scope)


class ContextComposition(unittest.TestCase):
    def compose(self, page, rows, **kwargs):
        return compose_background(page, rows, tokens=1200, chars=4000, tokenizer="o200k_base", **kwargs)[:2]

    def test_whole_page_whole_facts_and_wrapper_share_budget(self):
        page = native_model()
        rows = [{"id": str(n), "text": "A complete fact. " * 30} for n in range(12)]
        block, code = self.compose(page, rows)
        self.assertEqual(code, "")
        self.assertIn(page["content"], block)
        self.assertLessEqual(len(block), 4000)
        self.assertLessEqual(token_count(block), 1200)
        self.assertIn("A complete fact. " * 30, block)

    def test_large_page_is_skipped_without_clipping_or_summarization(self):
        page = native_model(content="unique-native-page " * 1000)
        block, code = self.compose(page, [{"id": "fact", "text": "full fact"}])
        self.assertEqual(code, "background_too_large")
        self.assertNotIn("unique-native-page", block)
        self.assertIn("full fact", block)

    def test_content_null_and_partial_failures_do_not_claim_latest_or_no_memory(self):
        block, code = self.compose(native_model(content=""), [{"text": "fact"}])
        self.assertEqual(code, "background_empty")
        self.assertIn("fact", block)
        block, code = self.compose(native_model(), [], recall_code="recall_timeout")
        self.assertIn("A short native background.", block)
        self.assertIn("recall_timeout", block)
        self.assertNotIn("latest", block)

    def test_small_limits_include_status_and_document_identifiers(self):
        block, *_ = compose_background(
            native_model(), [{"text": "fact" * 50}], tokens=256, chars=256,
            tokenizer="o200k_base", page_code="background_timeout", recall_code="recall_timeout",
        )
        self.assertLessEqual(len(block), 256)
        self.assertLessEqual(token_count(block), 256)

    def test_unusable_facts_are_not_counted_as_success_and_background_flag_matches_content(self):
        block, code, facts_used, background_used = compose_background(
            native_model(), [{"text": "huge fact " * 5000}], tokens=256, chars=256,
            tokenizer="o200k_base",
        )
        self.assertFalse(facts_used)
        self.assertEqual(background_used, "A short native background." in block)
        self.assertIn("recall_content_unavailable", block)
