"""Upgrade preparation uses actual ownership and preserves maintenance on failure."""

from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from plugins.astrbot_plugin_hindsight_bridge.core.client import HindsightClient, HindsightError
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming, sha256
from plugins.astrbot_plugin_hindsight_bridge.core.maintenance import Maintenance
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox
from plugins.astrbot_plugin_hindsight_bridge.core.panel import MemoryPanel, PanelError


class ServiceUpgrade(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = HindsightClient("http://memory.test", "valid-key")
        self.client.ensure_auth = AsyncMock()
        self.client.request = AsyncMock(return_value={"api_version": "1.1.0"})
        self.client.bank_exists = AsyncMock(return_value=True)
        self.client.bank_list_contains = AsyncMock(return_value=False)
        self.client.list_operations = AsyncMock(return_value=[])
        self.queue = Outbox(self.root / "runtime", "test", self.client.url,
                            credential_fingerprint=sha256(self.client.api_key), control_directory=self.root / "control")
        self.rows = [dict(self.queue.scope(Incoming("aiocqhttp", "onebot", "bot", user, "1", 1, "hello")))
                     for user in ("alice", "bob")]
        self.maintenance = Maintenance(self.queue, self.client)

        @asynccontextmanager
        async def resources():
            yield

        self.plugin = SimpleNamespace(context=SimpleNamespace(), client=self.client, outbox=self.queue,
                                      config={"api_url": self.client.url, "api_key": self.client.api_key},
                                      using_resources=resources, stopping=False,
                                      refresh_permissions=lambda: None,
                                      worker=SimpleNamespace(maintenance=self.maintenance, wake=SimpleNamespace(set=lambda: None)))
        self.panel = MemoryPanel(self.plugin)
        self.panel.directory = lambda: self.root / "setup"
        self.panel.store_state({"id": "012345abcdef", "api_url": self.client.url,
                                "database_password": "database-secret", "api_key": self.client.api_key,
                                "management_key": "management-secret", "management_url": "http://memory.test:8766"})
        self.panel.managed_request = AsyncMock(return_value={"ready": True, "embedding_fingerprint": "original"})

    async def asyncTearDown(self):
        await self.client.close()
        self.queue.close()
        self.temp.cleanup()

    async def action(self, action, **extra):
        self.panel.body = AsyncMock(return_value={"action": action, "config_revision": self.panel.config_revision(), **extra})
        with patch.dict(sys.modules, {"astrbot.api.web": SimpleNamespace(request=SimpleNamespace(username="admin"))}):
            return await self.panel.upgrade()

    async def test_versions_are_actual_cached_per_identity_and_unknown_is_not_default(self):
        status = await self.panel.service_status(force=False)
        self.assertTrue(status["upgrade_available"])
        await self.panel.service_status(force=False)
        self.assertEqual(self.client.request.await_count, 1)
        self.client.request.return_value = {"api_version": "1.2.0"}
        self.assertFalse((await self.panel.service_status())["upgrade_available"])
        self.client.request.return_value = {"api_version": "1.2.x"}
        self.assertIsNone((await self.panel.service_status())["version"])
        self.client.request.side_effect = HindsightError("http_503")
        self.assertIsNone((await self.panel.service_status())["version"])

    async def test_prepare_requires_backups_all_banks_and_retains_pending_data(self):
        with self.assertRaises(PanelError):
            await self.action("prepare")
        with self.assertRaises(PanelError):
            await self.action("prepare", backup_confirmed=True)
        await self.action("begin")
        self.assertTrue(all(self.queue.summary(row["scope_id"])["maintenance"] for row in self.rows))
        self.client.list_operations.side_effect = HindsightError("http_503")
        with self.assertRaises(HindsightError):
            await self.action("prepare", backup_confirmed=True)
        self.client.list_operations.side_effect = None
        result = await self.action("prepare", backup_confirmed=True)
        self.assertTrue(result["command"])
        self.assertEqual(len(self.panel.state()["upgrade_check"]["banks"]), 2)
        self.assertFalse(self.panel.state()["upgrade_check"]["verified"])

    async def test_verify_checks_old_banks_model_and_identity_and_survives_reload(self):
        await self.action("begin")
        await self.action("prepare", backup_confirmed=True)
        self.panel = self.reload_panel()
        with self.assertRaises(PanelError):
            await self.action("verify")
        self.client.request.return_value = {"api_version": "1.2.0"}
        self.client.bank_exists.return_value = False
        with self.assertRaises(PanelError):
            await self.action("verify")
        self.client.bank_exists.return_value = True
        self.panel.managed_request.return_value["embedding_fingerprint"] = "changed"
        with self.assertRaises(PanelError):
            await self.action("verify")
        self.panel.managed_request.return_value["embedding_fingerprint"] = "original"
        result = await self.action("verify")
        self.assertTrue(result["verified"])
        self.assertTrue(all(self.queue.summary(row["scope_id"])["maintenance"] for row in self.rows))

    def reload_panel(self):
        panel = MemoryPanel(self.plugin)
        panel.directory = lambda: self.root / "setup"
        panel.managed_request = self.panel.managed_request
        return panel

    def reopen_queue(self):
        self.queue.close()
        self.queue = Outbox(self.root / "runtime", "test", self.client.url,
                            credential_fingerprint=sha256(self.client.api_key), control_directory=self.root / "control")
        self.maintenance = Maintenance(self.queue, self.client)
        self.plugin.outbox = self.queue
        self.plugin.worker.maintenance = self.maintenance

    async def test_begin_save_failure_never_copies_uncommitted_control_revision(self):
        store = self.panel.store_state
        for written in (False, True):
            with self.subTest(setup_written=written):
                def fail(value):
                    if written:
                        store(value)
                    raise OSError("setup save interrupted")

                with patch.object(self.panel, "store_state", side_effect=fail):
                    with self.assertRaises(OSError):
                        await self.action("begin")
                self.assertLessEqual(int(self.queue.setting("control_revision")),
                                     int(self.queue.control.setting("revision")))
                self.reopen_queue()
                self.assertFalse(self.queue.recovering)
                self.assertTrue(all(self.queue.summary(row["scope_id"])["maintenance"] is None for row in self.rows))
                await self.action("begin")
                self.assertEqual(len(self.panel.state()["upgrade_pauses"]["banks"]), 2)
                await self.action("cancel")
                self.assertTrue(all(self.queue.summary(row["scope_id"])["maintenance"] is None for row in self.rows))

    async def test_begin_committed_control_replays_after_runtime_write_failure(self):
        with patch.object(self.queue, "apply_control", side_effect=OSError("runtime write interrupted")):
            with self.assertRaises(OSError):
                await self.action("begin")
        self.assertTrue(all(self.queue.control.maintenance(self.queue.connection_id, row["bank"]) for row in self.rows))
        self.reopen_queue()
        self.assertTrue(self.queue.recovering)
        await self.maintenance.startup()
        self.assertTrue(all(self.queue.summary(row["scope_id"])["maintenance"] for row in self.rows))
        await self.action("cancel")
        self.assertTrue(all(self.queue.summary(row["scope_id"])["maintenance"] is None for row in self.rows))

    async def test_external_service_and_unknown_versions_cannot_prepare_and_cancel_is_safe(self):
        self.panel.store_state({**self.panel.state(), "api_url": "http://other.test"})
        with self.assertRaises(PanelError):
            await self.action("begin")
        self.panel.store_state({**self.panel.state(), "api_url": self.client.url})
        await self.action("begin")
        await self.action("prepare", backup_confirmed=True)
        self.client.request.return_value = {"api_version": "unknown"}
        with self.assertRaises(PanelError):
            await self.action("cancel")
        self.client.request.return_value = {"api_version": "1.1.0"}
        await self.action("cancel")
        self.assertNotIn("upgrade_check", self.panel.state())
        self.assertTrue(all(not self.queue.summary(row["scope_id"])["maintenance"] for row in self.rows))

    async def test_bulk_resume_preserves_preexisting_pause_readonly_and_paused_modes(self):
        self.queue.set_mode(self.rows[0]["scope_id"], "readonly")
        self.maintenance.begin(self.rows[1]["scope_id"], management=self.queue.management(self.rows[1]["scope_id"]))
        await self.action("begin")
        self.assertEqual(len(self.panel.state()["upgrade_pauses"]["banks"]), 1)
        await self.action("prepare", backup_confirmed=True)
        self.panel = self.reload_panel()
        self.client.request.return_value = {"api_version": "1.2.0"}
        with self.assertRaises(PanelError):
            await self.action("resume")
        await self.action("verify")
        status = await self.panel.service_status()
        self.assertTrue(status["resume_available"])
        result = await self.action("resume")
        self.assertEqual(result["resumed"], 1)
        self.assertIsNone(self.queue.summary(self.rows[0]["scope_id"])["maintenance"])
        self.assertTrue(self.queue.summary(self.rows[1]["scope_id"])["maintenance"])
        self.assertEqual(self.queue.scope_owner(self.rows[0]["scope_id"])["mode"], "readonly")
        self.assertFalse((await self.panel.service_status())["upgrade_paused"])

    async def test_cancelling_before_command_restores_only_upgrade_pauses(self):
        self.queue.set_mode(self.rows[0]["scope_id"], "paused")
        self.maintenance.begin(self.rows[1]["scope_id"], management=self.queue.management(self.rows[1]["scope_id"]))
        await self.action("begin")
        await self.action("begin")
        self.panel = self.reload_panel()
        result = await self.action("cancel")
        self.assertNotIn("upgrade_pauses", self.panel.state())
        self.assertIsNone(self.queue.summary(self.rows[0]["scope_id"])["maintenance"])
        self.assertEqual(self.queue.scope_owner(self.rows[0]["scope_id"])["mode"], "paused")
        self.assertTrue(self.queue.summary(self.rows[1]["scope_id"])["maintenance"])
        self.assertIn("命令不会撤销", result["message"])

    async def test_bulk_resume_rechecks_every_bank_before_releasing_any_pause(self):
        await self.action("begin")
        await self.action("prepare", backup_confirmed=True)
        self.client.request.return_value = {"api_version": "1.2.0"}
        await self.action("verify")
        self.client.bank_exists.side_effect = [True, False]
        # Existing banks cannot be silently replaced, even at the final batch read.
        self.queue.control.set_bank_state(self.queue.connection_id, self.rows[1]["bank"], "established")
        with self.assertRaises(PanelError):
            await self.action("resume")
        self.assertTrue(all(self.queue.summary(row["scope_id"])["maintenance"] for row in self.rows))
        self.client.bank_exists.side_effect = None

        changed = False
        async def mutate(bank):
            nonlocal changed
            if not changed:
                changed = True
                self.queue.set_mode(self.rows[0]["scope_id"], "paused")
            return True
        self.client.bank_exists.side_effect = mutate
        with self.assertRaises(PanelError):
            await self.action("resume")
        self.assertTrue(all(self.queue.summary(row["scope_id"])["maintenance"] for row in self.rows))

    async def test_explicit_personal_pause_is_not_released_by_bulk_resume(self):
        await self.action("begin")
        await self.action("prepare", backup_confirmed=True)
        original_revision = self.panel.config_revision()
        self.panel.body = AsyncMock(return_value={"action": "maintenance_begin", "config_revision": original_revision})
        with patch.dict(sys.modules, {"astrbot.api.web": SimpleNamespace(request=SimpleNamespace(username="admin", method="POST"))}):
            response = await self.panel.action(self.rows[0]["scope_id"])
        self.assertNotEqual(response["config_revision"], original_revision)
        self.assertEqual(response["config_revision"], self.panel.config_revision())
        with patch.dict(sys.modules, {"astrbot.api.web": SimpleNamespace(request=SimpleNamespace(username="admin", method="POST"))}):
            with self.assertRaisesRegex(PanelError, "配置已被更新"):
                await self.panel.action(self.rows[0]["scope_id"])
        self.assertEqual(len(self.panel.state()["upgrade_pauses"]["banks"]), 1)
        self.client.request.return_value = {"api_version": "1.2.0"}
        await self.action("verify")
        self.assertEqual((await self.action("resume"))["resumed"], 1)
        self.assertTrue(self.queue.summary(self.rows[0]["scope_id"])["maintenance"])
        self.assertIsNone(self.queue.summary(self.rows[1]["scope_id"])["maintenance"])

    async def test_personal_pause_intent_survives_failure_before_control_update(self):
        await self.action("begin")
        await self.action("prepare", backup_confirmed=True)
        self.panel.body = AsyncMock(return_value={"action": "maintenance_begin",
                                                  "config_revision": self.panel.config_revision()})
        revision = self.queue.control.setting("revision")
        modules = {"astrbot.api.web": SimpleNamespace(request=SimpleNamespace(username="admin", method="POST"))}
        with patch.dict(sys.modules, modules), patch.object(self.panel, "store_state", side_effect=OSError("setup interrupted")):
            with self.assertRaises(OSError):
                await self.panel.action(self.rows[0]["scope_id"])
        self.assertEqual(self.queue.control.setting("revision"), revision)
        self.assertEqual(len(self.panel.state()["upgrade_pauses"]["banks"]), 2)
        with patch.dict(sys.modules, modules), patch.object(self.maintenance, "begin", side_effect=OSError("control interrupted")):
            with self.assertRaises(OSError):
                await self.panel.action(self.rows[0]["scope_id"])
        self.reopen_queue()
        self.panel = self.reload_panel()
        self.client.request.return_value = {"api_version": "1.2.0"}
        await self.action("verify")
        self.assertEqual((await self.action("resume"))["resumed"], 1)
        self.assertTrue(self.queue.summary(self.rows[0]["scope_id"])["maintenance"])
        self.assertIsNone(self.queue.summary(self.rows[1]["scope_id"])["maintenance"])

    async def test_legacy_upgrade_record_does_not_guess_the_source_of_pauses(self):
        await self.action("begin")
        await self.action("prepare", backup_confirmed=True)
        state = self.panel.state()
        state.pop("upgrade_pauses")
        self.panel.store_state(state)
        self.client.request.return_value = {"api_version": "1.2.0"}
        result = await self.action("verify")
        self.assertIn("未区分暂停来源", result["message"])
        self.assertEqual((await self.action("resume"))["resumed"], 0)
        self.assertTrue(all(self.queue.summary(row["scope_id"])["maintenance"] for row in self.rows))

    async def test_identity_change_during_prepare_does_not_issue_command(self):
        await self.action("begin")

        async def changed(bank):
            self.plugin.config["api_key"] = "different-key"
            return True

        self.client.bank_exists.side_effect = changed
        with self.assertRaises(PanelError):
            await self.action("prepare", backup_confirmed=True)
        self.assertNotIn("upgrade_check", self.panel.state())

    async def test_pending_upgrade_blocks_resume_until_verified_or_cancelled(self):
        await self.action("begin")
        await self.action("prepare", backup_confirmed=True)
        self.panel.body = AsyncMock(return_value={"action": "maintenance_resume"})
        with patch.dict(sys.modules, {"astrbot.api.web": SimpleNamespace(request=SimpleNamespace(username="admin", method="POST"))}):
            with self.assertRaises(PanelError):
                await self.panel.action(self.rows[0]["scope_id"])
        self.assertTrue(self.queue.summary(self.rows[0]["scope_id"])["maintenance"])

    async def test_repeated_begin_and_mode_change_do_not_invalidate_bank_identity(self):
        await self.action("begin")
        await self.action("prepare", backup_confirmed=True)
        revision = self.queue.scope_owner(self.rows[0]["scope_id"])["revision"]
        await self.action("begin")
        self.assertEqual(self.queue.scope_owner(self.rows[0]["scope_id"])["revision"], revision)
        self.queue.set_mode(self.rows[0]["scope_id"], "paused")
        self.client.request.return_value = {"api_version": "1.2.0"}
        self.assertTrue((await self.action("verify"))["verified"])
        await self.maintenance.resume(self.rows[0]["scope_id"], management=self.queue.management(self.rows[0]["scope_id"]))
        self.assertEqual(self.queue.scope_owner(self.rows[0]["scope_id"])["mode"], "paused")

    async def test_unknown_owned_version_cannot_generate_an_unprotected_upgrade(self):
        self.client.request.side_effect = HindsightError("http_503")
        self.panel.body = AsyncMock(return_value={})
        with self.assertRaises(PanelError):
            await self.panel.prepare()
        self.panel.body = AsyncMock(return_value={"repair": True})
        result = await self.panel.prepare()
        from test_installer import script
        self.assertIn("同版本修复仅支持1.2.0", script(result))
        self.assertNotIn("upgrade_check", self.panel.state())

    async def test_external_old_version_can_create_a_new_isolated_managed_installation(self):
        self.panel.store_state({})
        before = dict(self.plugin.config)
        self.panel.body = AsyncMock(return_value={})
        result = await self.panel.prepare()
        self.assertNotEqual(result["api_url"], self.client.url)
        self.assertEqual(self.plugin.config, before)
        self.assertTrue(self.panel.state()["id"])

    async def test_pending_upgrade_rejects_configuration_changes_and_real_generation_changes(self):
        await self.action("begin")
        await self.action("prepare", backup_confirmed=True)
        self.panel.body = AsyncMock(return_value={})
        with self.assertRaises(PanelError):
            await self.panel.save()
        state = self.panel.state()
        state["upgrade_check"]["banks"][0]["generation"] += 1
        self.panel.store_state(state)
        self.client.request.return_value = {"api_version": "1.2.0"}
        with self.assertRaises(PanelError):
            await self.action("verify")

    async def test_forget_during_last_prepare_http_read_cannot_issue_upgrade_command(self):
        await self.action("begin")
        changed = False

        async def forget(bank):
            nonlocal changed
            if not changed:
                changed = True
                self.queue.forget(self.rows[0]["scope_id"])
            return True

        self.client.bank_exists.side_effect = forget
        with self.assertRaises(PanelError):
            await self.action("prepare", backup_confirmed=True)
        self.assertNotIn("upgrade_check", self.panel.state())

    async def test_control_changes_during_verify_cannot_mark_upgrade_verified(self):
        for change in ("forget", "new_scope"):
            with self.subTest(change=change):
                # Reopen a fresh fixture for each independently altered control journal.
                if change != "forget":
                    await self.asyncTearDown()
                    self.setUp()
                await self.action("begin")
                await self.action("prepare", backup_confirmed=True)
                self.client.request.return_value = {"api_version": "1.2.0"}
                changed = False

                async def mutate(bank):
                    nonlocal changed
                    if not changed:
                        changed = True
                        if change == "forget":
                            self.queue.forget(self.rows[0]["scope_id"])
                        else:
                            self.queue.scope(Incoming("aiocqhttp", "onebot", "bot", "carol", "1", 1, "hello"))
                    return True

                self.client.bank_exists.side_effect = mutate
                with self.assertRaises(PanelError):
                    await self.action("verify")
                self.assertFalse(self.panel.state()["upgrade_check"]["verified"])

    async def test_connection_wide_inflight_or_late_cleanup_blocks_prepare_and_verify(self):
        for action in ("prepare", "verify"):
            with self.subTest(action=action):
                if action == "verify":
                    await self.asyncTearDown()
                    self.setUp()
                erasure = self.queue.forget(self.rows[0]["scope_id"])
                self.queue.control.cleanup_state(erasure["forget_id"], "verified", step=7)
                self.maintenance.check = AsyncMock(return_value=True)
                await self.action("begin")
                self.queue.in_flight.add("retired-bank-request")
                with self.assertRaises(PanelError):
                    await self.action("prepare", backup_confirmed=True)
                self.queue.in_flight.clear()
                if action == "verify":
                    await self.action("prepare", backup_confirmed=True)
                    self.client.request.return_value = {"api_version": "1.2.0"}
                revision = self.queue.control.setting("revision")

                async def late_request(bank):
                    self.queue.control.retired_request_returned(self.queue.connection_id, erasure["old_bank"])
                    return True

                self.client.bank_exists.side_effect = late_request
                with self.assertRaises(PanelError):
                    await self.action(action, backup_confirmed=True)
                self.assertEqual(self.queue.control.setting("revision"), revision)
                self.assertEqual(self.queue.control.erasures(self.queue.connection_id)[0]["phase"], "pending")
                self.assertFalse(self.panel.state().get("upgrade_check", {}).get("verified", False))

    async def test_missing_established_bank_is_rejected_but_never_created_bank_can_upgrade(self):
        await self.action("begin")
        self.client.bank_exists.return_value = False
        bank = self.rows[0]["bank"]
        self.queue.control.set_bank_state(self.queue.connection_id, bank, "established")
        with self.assertRaises(PanelError):
            await self.action("prepare", backup_confirmed=True)
        self.assertNotIn("upgrade_check", self.panel.state())
        self.queue.control.set_bank_state(self.queue.connection_id, bank, "new")
        self.client.bank_list_contains.return_value = True
        with self.assertRaises(PanelError):
            await self.action("prepare", backup_confirmed=True)
        self.client.bank_list_contains.return_value = False
        await self.action("prepare", backup_confirmed=True)
        self.assertTrue(all(not row["existed"] for row in self.panel.state()["upgrade_check"]["banks"]))
        self.client.request.return_value = {"api_version": "1.2.0"}
        self.assertTrue((await self.action("verify"))["verified"])
