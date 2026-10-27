"""Maintenance races and stale snapshots require controlled HTTP failures."""

import asyncio
import logging
import math
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import httpx
from test_controls import MemoryService

from plugins.astrbot_plugin_hindsight_bridge.core.client import HindsightClient
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming, sha256
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox, QueueError
from plugins.astrbot_plugin_hindsight_bridge.core.worker import Worker


class MaintenanceService(MemoryService):
    fail_config_once = False

    async def __call__(self, request):
        if request.url.path.endswith("/config") and self.fail_config_once:
            self.fail_config_once = False
            return httpx.Response(503, json={})
        return await super().__call__(request)


class BankMaintenance(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = MaintenanceService()
        self.client = HindsightClient(
            "http://memory.test", "valid-key", transport=httpx.MockTransport(self.service)
        )
        self.open()
        self.source = Incoming(
            "aiocqhttp", "onebot", "bot", "alice", "first",
            int(time.time()), "Remember this conversation.",
        )

    def open(self):
        self.queue = Outbox(
            self.root / "runtime", "test", "http://memory.test",
            credential_fingerprint=sha256("valid-key"),
            control_directory=self.root / "control",
        )
        self.worker = Worker(
            self.queue, self.client, logging.getLogger("maintenance"), poll_interval=0.01
        )

    async def asyncTearDown(self):
        await self.client.close()
        self.queue.close()
        self.temp.cleanup()

    def enqueue(self, message="first"):
        capture = self.queue.capture(replace(self.source, message_id=message), "conversation")
        self.queue.release(capture.task_id)
        return capture

    async def step(self):
        with self.queue.db:
            self.queue.db.execute("UPDATE outbox SET next_at=0")
        return await self.worker.step()

    async def seed(self):
        captured = self.enqueue()
        await self.step()
        await self.step()
        self.assertEqual(self.queue.get(captured.task_id)["state"], "done")
        return captured

    async def test_deleted_established_bank_is_not_implicitly_recreated(self):
        first = await self.seed()
        pending = self.enqueue("second")
        await self.client.delete_bank(first.bank)
        put_count = sum(method == "PUT" for method, _ in self.service.requests)
        await self.step()
        self.assertEqual(self.queue.get(pending.task_id)["last_error"], "bank_missing")
        self.assertTrue(self.queue.summary(first.scope_id)["bank_missing"])
        self.assertEqual(sum(method == "PUT" for method, _ in self.service.requests), put_count)
        self.assertEqual(len(self.service.submissions), 1)
        self.assertFalse(self.queue.can_recall(self.queue.scope(self.source)))
        with self.assertRaisesRegex(QueueError, "bank_missing"):
            self.queue.set_mode(first.scope_id, "active")

    async def test_maintenance_waits_for_inflight_acceptance_and_blocks_late_callbacks(self):
        self.service.admit_gate = asyncio.Event()
        first = self.enqueue()
        old_scope = self.queue.scope(self.source)
        running = asyncio.create_task(self.worker.step())
        await asyncio.wait_for(self.service.submit_started.wait(), 1)
        self.worker.maintenance.begin(first.scope_id, discard_pending=True)
        self.assertFalse((await self.worker.maintenance.status(first.scope_id))["ready"])
        self.assertFalse(self.queue.can_recall(old_scope))
        with self.assertRaisesRegex(QueueError, "scope_not_active"):
            self.queue.capture_assistant(first.task_id, "late reply")
        with self.assertRaisesRegex(QueueError, "scope_not_active"):
            self.enqueue("during-maintenance")
        self.service.admit_gate.set()
        await running
        await self.step()
        self.assertTrue((await self.worker.maintenance.status(first.scope_id))["ready"])
        await self.worker.maintenance.resume(first.scope_id)
        self.assertFalse(self.queue.can_recall(old_scope))
        with self.assertRaisesRegex(QueueError, "scope_not_active"):
            self.queue.capture_assistant(first.task_id, "reply before maintenance")

    async def test_pending_work_requires_explicit_discard_before_native_delete(self):
        first = await self.seed()
        pending = self.enqueue("pending")
        self.worker.maintenance.begin(first.scope_id)
        status = await self.worker.maintenance.status(first.scope_id)
        self.assertEqual(status["pending"], 1)
        self.assertFalse(status["ready"])
        with self.assertRaisesRegex(QueueError, "maintenance_active"):
            self.queue.set_mode(first.scope_id, "active")
        self.assertFalse(await self.step())
        self.worker.maintenance.begin(first.scope_id, discard_pending=True)
        self.assertEqual(self.queue.get(pending.task_id)["state"], "discarded")
        self.assertTrue((await self.worker.maintenance.status(first.scope_id))["ready"])
        await self.client.delete_bank(first.bank)
        with self.assertRaisesRegex(ValueError, "bank_missing"):
            await self.worker.maintenance.resume(first.scope_id)
        self.assertNotIn(first.bank, self.service.banks)

    async def test_old_runtime_snapshot_cannot_replay_discarded_work_after_resume(self):
        first = await self.seed()
        pending = self.enqueue("pending")
        snapshot = self.root / "old.sqlite3"
        with closing(sqlite3.connect(snapshot)) as db:
            self.queue.db.backup(db)
        self.worker.maintenance.begin(first.scope_id, discard_pending=True)
        self.assertTrue((await self.worker.maintenance.status(first.scope_id))["ready"])
        self.service.documents[first.bank].clear()
        await self.worker.maintenance.resume(first.scope_id)
        self.queue.close()
        with closing(sqlite3.connect(snapshot)) as old, closing(
            sqlite3.connect(self.root / "runtime" / "runtime.sqlite3")
        ) as destination:
            old.backup(destination)
        self.open()
        await self.step()
        self.assertIn(self.queue.get(pending.task_id)["state"], {"cancelled", "discarded"})
        self.assertEqual(len(self.service.submissions), 1)

    async def test_unknown_unaccepted_request_is_not_resubmitted_during_maintenance(self):
        pending = self.enqueue()
        self.service.fail_before_accept = True
        await self.step()
        self.worker.maintenance.begin(pending.scope_id, discard_pending=True)
        await self.step()
        self.assertEqual(self.queue.get(pending.task_id)["last_error"],
                         "maintenance_unknown_operation")
        self.assertFalse((await self.worker.maintenance.status(pending.scope_id))["ready"])
        self.assertEqual(len(self.service.submissions), 1)
        erasure = self.queue.forget(pending.scope_id)
        self.assertTrue(await self.worker.maintenance.check(force=True))
        self.queue.set_mode(pending.scope_id, "active", now=math.floor(time.time()))
        self.assertNotEqual(self.queue.scope(self.source)["bank"], pending.bank)
        self.assertNotIn(erasure["old_bank"], self.service.banks)

    async def test_lost_acceptance_reply_is_tracked_without_replaying(self):
        first = self.enqueue()
        self.service.lose_ack = True
        await self.step()
        self.worker.maintenance.begin(first.scope_id, discard_pending=True)
        await self.step()
        self.assertTrue((await self.worker.maintenance.status(first.scope_id))["ready"])
        self.assertEqual(len(self.service.submissions), 1)

    async def test_cancelled_unknown_submission_still_blocks_destructive_maintenance(self):
        pending = self.enqueue()
        self.service.fail_before_accept = True
        await self.step()
        self.queue.request_cancel(pending.scope_id, pending.task_id)
        self.worker.maintenance.begin(pending.scope_id, discard_pending=True)
        await self.step()
        self.assertEqual(self.queue.get(pending.task_id)["state"], "cancelled")
        self.assertTrue(self.queue.control.request(
            self.queue.get(pending.task_id)["operation_id"]
        )["dispatched"])
        self.assertFalse((await self.worker.maintenance.status(pending.scope_id))["ready"])
        self.assertEqual(len(self.service.submissions), 1)

    async def test_first_creation_read_failure_keeps_new_bank_retryable(self):
        pending = self.enqueue()
        self.service.fail_config_once = True
        await self.step()
        self.assertEqual(self.queue.control.bank_state("test", pending.bank), "new")
        self.assertFalse(self.queue.summary(pending.scope_id)["bank_missing"])
        await self.step()
        self.assertEqual(self.queue.get(pending.task_id)["state"], "tracking")
        self.assertIn(pending.bank, self.service.banks)

    async def test_maintenance_is_persistent_and_does_not_block_other_accounts(self):
        first = await self.seed()
        self.worker.maintenance.begin(first.scope_id)
        self.queue.close()
        self.open()
        self.assertFalse(self.queue.can_recall(self.queue.scope(self.source)))
        with self.assertRaisesRegex(QueueError, "scope_not_active"):
            self.enqueue("after-restart")
        other = self.queue.capture(replace(self.source, user_id="bob"), "conversation")
        self.queue.release(other.task_id)
        await self.step()
        self.assertEqual(self.queue.get(other.task_id)["state"], "tracking")
