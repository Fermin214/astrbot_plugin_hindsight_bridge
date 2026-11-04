"""Actual bridge methods with controlled native HTTP and host I/O."""

import asyncio
import json
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import test_runtime_diagnostics as runtime
from test_panel import InMemoryPanel
from test_background import native_model, native_tree
from plugins.astrbot_plugin_hindsight_bridge.core.background import reference, save_reference
from plugins.astrbot_plugin_hindsight_bridge.core.control import QueueError
from plugins.astrbot_plugin_hindsight_bridge.core.maintenance import Maintenance


class BackgroundHooks(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = runtime.RuntimeDiagnostics.asyncSetUp
    asyncTearDown = runtime.RuntimeDiagnostics.asyncTearDown
    turn = runtime.RuntimeDiagnostics.turn

    async def service(self, request):
        self.requests.append(request)
        if request.headers.get("Authorization") != "Bearer key":
            return httpx.Response(401, json={})
        if request.url.path.endswith("/recall"):
            if getattr(self, "fact_status", 200) != 200:
                return httpx.Response(self.fact_status, json={})
            if getattr(self, "fact_gate", None):
                await self.fact_gate()
            return httpx.Response(200, json={"results": self.results})
        if request.url.path.endswith("/mental-models/mm-1"):
            if getattr(self, "page_status", 200) != 200:
                return httpx.Response(self.page_status, json={})
            if getattr(self, "page_gate", None):
                await self.page_gate()
            return httpx.Response(200, json=native_model(
                self.scope()["bank"], getattr(self, "page_content", "Native background marker."),
            ))
        if request.url.path.endswith("/knowledge-base/tree"):
            if getattr(self, "tree_gate", None):
                await self.tree_gate()
            return httpx.Response(200, json=native_tree())
        if request.url.path.endswith("/knowledge-base/pages/kp-1"):
            if getattr(self, "native_page_gate", None):
                await self.native_page_gate()
            return httpx.Response(200, json={
                "id": "kp-1", "name": "Background", "body": "Native background marker.",
            })
        if request.url.path.endswith("/config") and getattr(self, "bank_missing", False):
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"banks": []})

    def scope(self):
        return self.plugin.outbox.scope(self.incoming)

    def select(self, *, enabled=True, revision=0):
        return save_reference(
            self.plugin.outbox, self.scope(), revision=revision,
            page_id="kp-1", model_id="mm-1", enabled=enabled,
        )

    async def test_enabled_background_and_recall_are_temporary_and_not_frozen_material(self):
        self.select()
        self.results = [{"id": "fact", "text": "Native fact marker.", "type": "world"}]
        event, req = self.turn()
        await self.plugin.add_memory(event, req)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        block = req.extra_user_content_parts[0]
        self.assertTrue(block._no_save)
        self.assertIn("Native background marker.", block.text)
        self.assertIn("Native fact marker.", block.text)
        self.assertTrue(all(request.method == "GET" or request.url.path.endswith("/recall") for request in self.requests))
        row = self.plugin.outbox.db.execute("SELECT * FROM outbox LIMIT 1").fetchone()
        self.assertNotIn("Native background marker.", row["payload"])
        self.assertEqual(row["state"], "ready")
        self.assertEqual(self.plugin.hooks, 0)
        await self.plugin.add_memory(event, req)
        self.assertEqual(len(req.extra_user_content_parts), 1)

    async def test_default_off_uses_existing_recall_without_page_request(self):
        event, req = self.turn()
        await self.plugin.add_memory(event, req)
        self.assertFalse(any("mental-models" in str(call.url) for call in self.requests))
        self.assertEqual(len(req.extra_user_content_parts), 1)

    async def test_background_timeout_preserves_facts_and_releases_capture(self):
        self.select()
        self.config["recall_timeout"] = 0.2
        self.page_gate = lambda: asyncio.sleep(0.5)
        self.results = [{"id": "fact", "text": "Full surviving fact."}]
        event, req = self.turn()
        started = time.monotonic()
        await self.plugin.add_memory(event, req)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertIn("Full surviving fact.", req.extra_user_content_parts[0].text)
        self.assertNotIn("Native background marker.", req.extra_user_content_parts[0].text)
        self.assertEqual(self.plugin.outbox.db.execute("SELECT state FROM outbox").fetchone()[0], "ready")

    async def test_recall_timeout_preserves_completed_background_with_failure_status(self):
        self.select()
        self.config["recall_timeout"] = 0.2
        self.fact_gate = lambda: asyncio.sleep(0.5)
        event, req = self.turn()
        await self.plugin.add_memory(event, req)
        self.assertIn("Native background marker.", req.extra_user_content_parts[0].text)
        self.assertIn("recall_timeout", req.extra_user_content_parts[0].text)
        self.assertEqual(self.plugin.recall_status(self.scope())["outcome"], "error")

    async def test_known_auth_failure_discards_background_already_read_this_turn(self):
        self.select()
        self.fact_status = 401
        event, req = self.turn()
        await self.plugin.add_memory(event, req)
        self.assertNotIn("Native background marker.", req.extra_user_content_parts[0].text)
        self.assertEqual(self.plugin.recall_status(self.scope())["code"], "http_401")
        self.assertFalse(next(iter(self.plugin.background_outcomes.values()))["used"])

    async def test_background_auth_failure_also_discards_facts_and_pauses_connection(self):
        self.select()
        self.page_status = 403
        self.results = [{"id": "fact", "text": "Must not survive authentication failure."}]
        event, req = self.turn()
        await self.plugin.add_memory(event, req)
        self.assertNotIn("Must not survive", req.extra_user_content_parts[0].text)
        self.assertEqual(self.plugin.recall_status(self.scope())["code"], "http_403")
        self.assertFalse(next(iter(self.plugin.background_outcomes.values()))["used"])
        self.assertEqual(self.plugin.outbox.db.execute("SELECT paused_error FROM connections").fetchone()[0], "http_403")

    async def test_missing_bank_replays_control_and_keeps_readonly_management_reachable(self):
        self.select()
        queue = self.plugin.outbox
        queue.set_mode(self.scope()["scope_id"], "readonly")
        before = self.scope()
        queue.control.set_bank_state(queue.connection_id, before["bank"], "established")
        self.page_status, self.bank_missing = 404, True
        event, req = self.turn()
        await self.plugin.add_memory(event, req)
        latest = self.scope()
        owner = queue.control.owner(queue.connection_id, latest["owner_id"])
        self.assertEqual(latest["revision"], before["revision"] + 1)
        self.assertEqual(latest["revision"], owner["revision"])
        self.assertEqual(queue.control.bank_state(queue.connection_id, latest["bank"]), "missing")
        self.assertFalse(queue.can_recall(latest, manual=True))
        self.assertEqual(req.extra_user_content_parts, [])
        self.assertEqual(queue.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        self.assertFalse(any(call.method != "GET" and not call.url.path.endswith("/recall") for call in self.requests))
        panel = self.panel(SimpleNamespace(method="POST", query={}, username="admin"))
        self.assertEqual(panel.scope(latest["scope_id"])["revision"], owner["revision"])
        maintenance = Maintenance(queue, self.plugin.client)
        maintenance.begin(latest["scope_id"], management=queue.management(latest["scope_id"]))
        panel.body = AsyncMock(return_value={
            "page_id": "kp-1", "enabled": False, "revision": 1,
            "scope_revision": self.scope()["revision"], "config_revision": panel.config_revision(),
        })
        reads = len(self.requests)
        saved = await panel.background(latest["scope_id"])
        self.assertFalse(saved["selection"]["enabled"])
        self.assertEqual(len(self.requests), reads)
        queue.forget(latest["scope_id"], management=queue.management(latest["scope_id"]))
        self.assertNotEqual(self.scope()["bank"], latest["bank"])
        self.assertFalse(reference(queue, self.scope())["page_id"])

    async def test_close_maintenance_forget_revoke_and_service_change_discard_late_page(self):
        for change in ("close", "maintenance", "forget", "revoke", "service", "budget"):
            with self.subTest(change=change):
                # Each race gets a fresh control and permission epoch.
                await self.asyncTearDown()
                await self.asyncSetUp()
                self.select()
                entered, release = asyncio.Event(), asyncio.Event()

                async def gate():
                    entered.set()
                    await release.wait()

                self.page_gate = gate
                event, req = self.turn(change)
                task = asyncio.create_task(self.plugin.add_memory(event, req))
                await entered.wait()
                scope, queue = self.scope(), self.plugin.outbox
                if change == "close":
                    self.select(enabled=False, revision=1)
                elif change == "maintenance":
                    queue.control.begin_maintenance(queue.connection_id, scope["owner_id"])
                    queue.apply_control()
                elif change == "forget":
                    queue.forget(scope["scope_id"])
                elif change == "revoke":
                    self.config["allowed_accounts"] = []
                elif change == "budget":
                    self.config["context_max_chars"] = 256
                else:
                    self.config["api_url"] = "http://other.test"
                release.set()
                await task
                self.assertEqual(req.extra_user_content_parts, [])
                self.assertEqual(self.plugin.hooks, 0)

    async def test_authentication_delay_does_not_start_page_or_second_full_deadline(self):
        self.select()
        self.config["recall_timeout"] = 0.2
        async def auth(**kwargs):
            await asyncio.sleep(1)
        self.plugin.client.ensure_auth = auth
        event, req = self.turn()
        await self.plugin.add_memory(event, req)
        self.assertFalse(any("mental-models" in str(call.url) for call in self.requests))
        self.assertEqual(self.plugin.recall_status(self.scope())["code"], "auth_timeout")

    async def test_cancelled_parallel_read_leaves_no_running_tasks_or_notice(self):
        self.select()
        entered = asyncio.Event()
        cancelled = []

        async def gate():
            entered.set()
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)

        self.page_gate = self.fact_gate = gate
        event, req = self.turn()
        task = asyncio.create_task(self.plugin.add_memory(event, req))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(cancelled), 2)
        self.assertEqual(req.extra_user_content_parts, [])
        self.assertEqual(self.plugin.hooks, 0)

    async def test_personal_view_is_explicit_read_only_and_allows_paused_owner(self):
        self.select(enabled=False)
        self.plugin.outbox.set_mode(self.scope()["scope_id"], "paused")
        event, _ = self.turn()
        event.plain_result = lambda text: text
        self.plugin.command_scope = lambda event: self.scope()
        results = [result async for result in self.plugin.memory_background(event)]
        self.assertEqual(len(results), 1)
        self.assertIn("Native background marker.", results[0])
        self.assertTrue(all(call.method == "GET" for call in self.requests))
        self.assertEqual(self.plugin.outbox.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    async def test_personal_view_does_not_truncate_large_content_as_complete(self):
        self.select()
        self.page_content = "private large native content " * 300
        event, _ = self.turn()
        event.plain_result = lambda text: text
        self.plugin.command_scope = lambda event: self.scope()
        results = [result async for result in self.plugin.memory_background(event)]
        self.assertNotIn("private large native content", results[0])
        self.assertIn("background_view_too_large", results[0])

    def panel(self, request):
        panel = InMemoryPanel(self.plugin)
        panel.saved_state = None
        panel.bot_id = AsyncMock(return_value="456")
        patcher = patch.dict(sys.modules, {"astrbot.api.web": SimpleNamespace(request=request)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return panel

    async def test_admin_index_and_save_use_same_bank_without_body_in_response(self):
        request = SimpleNamespace(method="GET", query={}, username="admin")
        panel = self.panel(request)
        response = await panel.background(self.scope()["scope_id"])
        self.assertNotIn("content", json.dumps(response))
        self.assertEqual(response["pages"][0]["page_id"], "kp-1")
        request.method = "POST"
        panel.body = AsyncMock(return_value={
            "page_id": "kp-1", "enabled": True, "revision": response["selection"]["revision"],
            "scope_revision": response["scope_revision"], "config_revision": response["config_revision"],
        })
        saved = await panel.background(self.scope()["scope_id"])
        self.assertTrue(saved["selection"]["enabled"])
        with self.assertRaisesRegex(QueueError, "background_changed"):
            await panel.background(self.scope()["scope_id"])

    async def test_admin_rejects_model_supplied_bank_and_stale_tabs_before_page_io(self):
        panel = self.panel(SimpleNamespace(method="POST", query={}, username="admin"))
        payload = {
            "page_id": "kp-1", "enabled": True, "revision": 0,
            "scope_revision": self.scope()["revision"], "config_revision": panel.config_revision(),
        }
        for update in ({"bank": "other-bank"}, {"scope_revision": -1}, {"config_revision": "old"}):
            panel.body = AsyncMock(return_value={**payload, **update})
            with self.assertRaisesRegex(QueueError, "background_changed"):
                await panel.background(self.scope()["scope_id"])
        self.assertEqual(self.requests, [])

    async def test_admin_close_works_without_hindsight_io_even_during_maintenance(self):
        self.select()
        scope = self.scope()
        self.plugin.outbox.control.begin_maintenance(self.plugin.outbox.connection_id, scope["owner_id"])
        self.plugin.outbox.apply_control()
        panel = self.panel(SimpleNamespace(method="POST", query={}, username="admin"))
        panel.body = AsyncMock(return_value={
            "page_id": "kp-1", "enabled": False, "revision": 1,
            "scope_revision": self.scope()["revision"], "config_revision": panel.config_revision(),
        })
        response = await panel.background(scope["scope_id"])
        self.assertFalse(response["selection"]["enabled"])
        self.assertEqual(self.requests, [])

    async def test_admin_mapping_changes_or_revocation_during_save_do_not_commit(self):
        for change in ("mapping", "revoke"):
            with self.subTest(change=change):
                panel = self.panel(SimpleNamespace(method="POST", query={}, username="admin"))
                panel.body = AsyncMock(return_value={
                    "page_id": "kp-1", "enabled": True, "revision": 0,
                    "scope_revision": self.scope()["revision"], "config_revision": panel.config_revision(),
                })
                if change == "mapping":
                    self.page_content = "changed while selecting"
                else:
                    async def revoke():
                        self.config["allowed_accounts"] = []
                    self.page_gate = revoke
                with self.assertRaisesRegex(QueueError, "background_changed"):
                    await panel.background(self.scope()["scope_id"])
                self.assertFalse(reference(self.plugin.outbox, self.scope())["enabled"])

    async def test_admin_invalidated_tree_or_page_stops_before_next_content_read(self):
        for boundary in ("tree", "page"):
            for change in ("maintenance", "revoke", "forget", "config", "bot"):
                with self.subTest(boundary=boundary, change=change):
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                    panel = self.panel(SimpleNamespace(method="POST", query={}, username="admin"))
                    scope = self.scope()
                    panel.body = AsyncMock(return_value={
                        "page_id": "kp-1", "enabled": True, "revision": 0,
                        "scope_revision": scope["revision"], "config_revision": panel.config_revision(),
                    })

                    async def invalidate():
                        if change == "maintenance":
                            self.plugin.outbox.control.begin_maintenance(
                                self.plugin.outbox.connection_id, scope["owner_id"],
                            )
                            self.plugin.outbox.apply_control()
                        elif change == "revoke":
                            self.config["allowed_accounts"] = []
                        elif change == "forget":
                            self.plugin.outbox.forget(scope["scope_id"])
                        elif change == "config":
                            self.config["control_plane_url"] = "http://new-panel.test"
                        else:
                            panel.bot_id.return_value = "another-bot"

                    setattr(self, "tree_gate" if boundary == "tree" else "native_page_gate", invalidate)
                    with self.assertRaisesRegex(QueueError, "background_changed"):
                        await panel.background(scope["scope_id"])
                    paths = [call.url.path for call in self.requests]
                    self.assertFalse(any("/mental-models/" in path for path in paths))
                    if boundary == "tree":
                        self.assertFalse(any("/knowledge-base/pages/" in path for path in paths))
                    else:
                        self.assertEqual(sum("/knowledge-base/pages/" in path for path in paths), 1)
                    self.assertFalse(reference(self.plugin.outbox, self.scope())["enabled"])
                    self.tree_gate = self.native_page_gate = None
