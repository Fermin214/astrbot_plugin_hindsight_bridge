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
from unittest.mock import patch

import httpx
from test_controls import MemoryService

from plugins.astrbot_plugin_hindsight_bridge.core.client import HindsightClient, HindsightError
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

    async def test_uncreated_bank_can_finish_maintenance_without_losing_first_creation(self):
        scope = self.queue.scope(self.source)
        bank, sid = scope["bank"], scope["scope_id"]
        self.worker.maintenance.begin(sid)
        self.assertTrue((await self.worker.maintenance.status(sid))["ready"])
        await self.worker.maintenance.resume(sid)
        self.assertEqual(self.queue.control.bank_state("test", bank), "new")
        self.assertNotIn(bank, self.service.banks)
        self.assertIsNone(self.queue.control.maintenance("test", bank))
        self.source = replace(self.source, timestamp=math.ceil(time.time()))
        captured = self.enqueue("after-maintenance")
        await self.step()
        self.assertEqual(self.queue.get(captured.task_id)["state"], "tracking")
        self.assertIn(bank, self.service.banks)

    async def test_new_bank_with_durable_dispatch_evidence_is_not_assumed_uncreated(self):
        captured = self.enqueue()
        row = self.queue.get(captured.task_id)
        owner = self.queue.scope_owner(captured.scope_id)
        self.queue.control.record_request(row, owner["owner_id"], dispatched=True)
        self.queue.update(captured.task_id, "discarded")
        self.worker.maintenance.begin(captured.scope_id)
        self.assertTrue((await self.worker.maintenance.status(captured.scope_id))["ready"])
        with self.assertRaisesRegex(QueueError, "bank_missing"):
            await self.worker.maintenance.resume(captured.scope_id)
        self.assertEqual(self.queue.control.bank_state("test", captured.bank), "missing")

    async def test_erasure_batches_survive_timeout_and_restart_with_late_children(self):
        captured = await self.seed()
        oid = self.queue.get(captured.task_id)["operation_id"]
        for index in range(140):
            key = f"history-{index:03}"
            self.service.operations[key] = {"operation_id": key, "status": "pending"}
            self.service.op_banks[key] = captured.bank
        record = self.queue.forget(captured.scope_id)
        control_path = self.queue.control.directory / "control.sqlite3"
        self.queue.close()
        # Reopen an original version-2 control store, without erasure checkpoints.
        with closing(sqlite3.connect(control_path)) as db, db:
            db.execute("DROP INDEX erasure_refs_cleanup")
            db.execute("ALTER TABLE erasures DROP COLUMN cleanup_step")
            db.execute("ALTER TABLE erasures DROP COLUMN cleanup_epoch")
            db.execute("ALTER TABLE erasure_refs DROP COLUMN checked_pass")
            db.execute("PRAGMA user_version=2")
        self.open()
        self.assertEqual(self.queue.control.db.execute("PRAGMA user_version").fetchone()[0], 3)
        operation, listing = self.client.operation, self.client.list_operations
        reads, listings = [], 0

        async def fault_once(bank, ref):
            reads.append(ref)
            if len(reads) == 8:
                raise TimeoutError()
            return await operation(bank, ref)

        async def late_children(bank):
            nonlocal listings
            listings += 1
            if listings == 2:
                self.service.add_child(bank, oid, "late-child")
                self.service.add_child(bank, "late-child", "late-grandchild")
            return [
                op for op in await listing(bank)
                if op.id not in {"late-child", "late-grandchild"}
            ]

        with (
            patch.object(self.client, "operation", side_effect=fault_once),
            patch.object(self.client, "list_operations", side_effect=late_children),
        ):
            self.assertFalse(await self.worker.maintenance.check(force=True))
            erasure = self.queue.control.erasures("test")[0]
            self.assertEqual(erasure["cleanup_step"], 1)
            self.assertEqual(erasure["last_error"], "cleanup_timeout")
            self.assertGreater(erasure["next_at"] - time.time(), 50)
            self.assertEqual(len(self.queue.control.cleanup_refs(record["forget_id"], "operation", 1)), 128)
            self.assertEqual(self.service.deleted, [])
            completed_ref = reads[0]
            self.queue.close()
            self.open()
            self.assertEqual(self.queue.control.erasures("test")[0]["cleanup_step"], 1)
            for _ in range(6):
                if await self.worker.maintenance.check(force=True):
                    break
                erasure = self.queue.control.erasures("test")[0]
                self.assertEqual(erasure["last_error"], "")
                self.assertLessEqual(erasure["next_at"] - time.time(), 2)
                self.assertGreater(erasure["next_at"], time.time() - 1)
            else:
                self.fail("erasure did not advance across bounded calls")
        self.assertEqual(reads.count(completed_ref), 3)
        self.assertEqual(reads.count("late-grandchild"), 2)
        self.assertIn("late-child", self.service.cancelled)
        self.assertIn("late-grandchild", self.service.cancelled)
        self.assertEqual(len(set(self.service.cancelled)), 142)
        self.assertEqual(listings, 4)
        self.assertEqual(self.queue.control.erasures("test")[0]["phase"], "verified")
        self.assertNotIn(captured.bank, self.service.banks)

    async def test_late_submit_during_final_verification_invalidates_old_checkpoint(self):
        captured = await self.seed()
        oid = self.queue.get(captured.task_id)["operation_id"]
        doc = self.queue.get(captured.task_id)["document_id"]
        self.queue.forget(captured.scope_id)
        bank_list, checks = self.client.bank_list_contains, 0

        async def late_ack(bank):
            nonlocal checks
            result = await bank_list(bank)
            checks += 1
            if checks == 2:
                self.service.banks.add(bank)
                self.service.operations[oid] = {"operation_id": oid, "status": "processing"}
                self.service.documents[bank] = {doc}
                self.queue.control.retired_request_returned("test", bank)
            return result

        with patch.object(self.client, "bank_list_contains", side_effect=late_ack):
            self.assertFalse(await self.worker.maintenance.check(force=True))
        erasure = self.queue.control.erasures("test")[0]
        self.assertEqual(erasure["phase"], "pending")
        self.assertEqual(erasure["cleanup_step"], 0)
        self.assertEqual(erasure["next_at"], 0)
        self.assertTrue(await self.worker.maintenance.check())
        self.assertNotIn(captured.bank, self.service.banks)
        self.assertIn(oid, self.service.cancelled)
        self.assertEqual(self.service.deleted.count(captured.bank), 2)

    async def test_verified_bank_rechecks_hidden_cancelled_operations_and_restored_refs(self):
        captured = await self.seed()
        oid = self.queue.get(captured.task_id)["operation_id"]
        record = self.queue.forget(captured.scope_id)
        self.assertTrue(await self.worker.maintenance.check(force=True))
        delete_bank = self.client.delete_bank

        async def cooperative_cancel_write(bank):
            await delete_bank(bank)
            self.service.operations[oid] = {"operation_id": oid, "status": "cancelled"}

        with self.queue.control.db:
            self.queue.control.db.execute("UPDATE erasures SET next_at=0")
        with patch.object(self.client, "delete_bank", side_effect=cooperative_cancel_write):
            self.assertFalse(await self.worker.maintenance.check())
        erasure = self.queue.control.erasures("test")[0]
        self.assertEqual(erasure["last_error"], "retired_data_present")
        self.assertNotEqual(erasure["phase"], "verified")
        self.assertTrue(await self.worker.maintenance.check(force=True))
        epoch = self.queue.control.erasures("test")[0]["cleanup_epoch"]
        with self.queue.control.db:
            self.queue.control.add_refs(record["forget_id"], [("document", "restored-document")])
        erasure = self.queue.control.erasures("test")[0]
        self.assertGreater(erasure["cleanup_epoch"], epoch)
        self.assertEqual(erasure["phase"], "pending")
        self.assertEqual(erasure["next_at"], 0)
        self.assertTrue(await self.worker.maintenance.check())

    async def checkpointed_erasure(self, kind):
        captured = await self.seed()
        record = self.queue.forget(captured.scope_id)
        fid = record["forget_id"]
        await self.client.delete_bank(captured.bank)
        with self.queue.control.db:
            self.queue.control.add_refs(
                fid, [(kind, f"absent-{index:03}") for index in range(129)]
            )
            # Restore a verification checkpoint written by the batched cleanup format.
            self.queue.control.db.execute(
                "UPDATE erasure_refs SET checked_pass=3 "
                "WHERE forget_id=? AND kind=? AND ref<'absent-128'",
                (fid, kind),
            )
            self.queue.control.cleanup_state(fid, "cleaning", step=6, delay=0)
        self.queue.close()
        self.open()
        return captured.bank, self.queue.control.erasures("test")[0]

    async def finish_cleanup(self):
        for _ in range(4):
            if await self.worker.maintenance.check(force=True):
                return
            self.assertEqual(self.queue.control.erasures("test")[0]["last_error"], "")
        self.fail("erasure did not finish across bounded cancellation batches")

    async def test_resumed_verification_detects_hidden_operation_writeback(self):
        bank, record = await self.checkpointed_erasure("operation")
        self.service.operations["absent-000"] = {
            "operation_id": "absent-000", "status": "cancelled",
        }
        self.service.op_banks["absent-000"] = bank
        self.assertEqual(await self.client.list_operations(bank), [])
        self.assertFalse(await self.worker.maintenance.erase(record))
        erasure = self.queue.control.erasures("test")[0]
        self.assertEqual(erasure["last_error"], "retired_data_present")
        self.assertNotEqual(erasure["phase"], "verified")
        await self.finish_cleanup()
        self.assertEqual((await self.client.operation(bank, "absent-000")).status, "not_found")

    async def test_resumed_verification_detects_hidden_document_writeback(self):
        bank, record = await self.checkpointed_erasure("document")
        self.service.documents[bank] = {"absent-000"}
        self.assertFalse(await self.worker.maintenance.erase(record))
        erasure = self.queue.control.erasures("test")[0]
        self.assertEqual(erasure["last_error"], "retired_data_present")
        self.assertNotEqual(erasure["phase"], "verified")
        await self.finish_cleanup()
        self.assertFalse(await self.client.document_exists(bank, "absent-000"))

    async def test_verification_timeout_does_not_reuse_absence_results(self):
        bank, record = await self.checkpointed_erasure("operation")
        operation = self.client.operation
        reads = []

        async def timeout_at_last_batch(bank, ref):
            reads.append(ref)
            if ref == "absent-128":
                raise TimeoutError()
            return await operation(bank, ref)

        with patch.object(self.client, "operation", side_effect=timeout_at_last_batch):
            self.assertFalse(await self.worker.maintenance.erase(record))
        erasure = self.queue.control.erasures("test")[0]
        self.assertEqual(erasure["last_error"], "cleanup_timeout")
        self.assertEqual(erasure["cleanup_step"], 6)
        self.assertIn("absent-000", reads)
        self.service.operations["absent-000"] = {
            "operation_id": "absent-000", "status": "cancelled",
        }
        self.service.op_banks["absent-000"] = bank
        self.assertFalse(await self.worker.maintenance.erase(erasure))
        self.assertEqual(
            self.queue.control.erasures("test")[0]["last_error"], "retired_data_present"
        )
        await self.finish_cleanup()

    async def test_verification_authentication_error_survives_mixed_batch_results(self):
        bank, record = await self.checkpointed_erasure("operation")
        operation = self.client.operation
        self.service.operations["absent-000"] = {
            "operation_id": "absent-000", "status": "cancelled",
        }
        self.service.op_banks["absent-000"] = bank

        async def mixed_results(bank, ref):
            if ref == "absent-001":
                raise TimeoutError()
            if ref == "absent-002":
                raise HindsightError("http_401")
            return await operation(bank, ref)

        with patch.object(self.client, "operation", side_effect=mixed_results):
            self.assertFalse(await self.worker.maintenance.erase(record))
        erasure = self.queue.control.erasures("test")[0]
        self.assertEqual(self.queue.connection_error(), "http_401")
        self.assertEqual(erasure["last_error"], "http_401")
        self.assertNotEqual(erasure["phase"], "verified")
        self.assertTrue(await self.worker.recover_connection())
        self.assertFalse(await self.worker.maintenance.check(force=True))
        self.assertEqual(
            self.queue.control.erasures("test")[0]["last_error"], "retired_data_present"
        )
        await self.finish_cleanup()

    async def test_complete_verification_can_outlast_one_batch_deadline(self):
        bank, record = await self.checkpointed_erasure("operation")
        operation = self.client.operation
        loop = asyncio.get_running_loop()
        clock, elapsed = loop.time, 0
        checked = set()

        async def slow_operation(bank, ref):
            nonlocal elapsed
            elapsed += 1
            checked.add(ref)
            await asyncio.sleep(0)
            return await operation(bank, ref)

        with (
            patch.object(loop, "time", side_effect=lambda: clock() + elapsed),
            patch.object(loop, "slow_callback_duration", float("inf")),
            patch.object(self.client, "operation", side_effect=slow_operation),
        ):
            self.assertTrue(await self.worker.maintenance.erase(record))
        self.assertGreater(elapsed, 45)
        self.assertTrue({f"absent-{index:03}" for index in range(129)} <= checked)
        self.assertEqual(self.queue.control.erasures("test")[0]["phase"], "verified")

    async def test_epoch_change_stops_obsolete_reference_verification(self):
        bank, record = await self.checkpointed_erasure("operation")
        operation, reads = self.client.operation, []

        async def late_return(bank, ref):
            reads.append(ref)
            if ref == "absent-000":
                self.queue.control.retired_request_returned("test", bank)
            return await operation(bank, ref)

        with patch.object(self.client, "operation", side_effect=late_return):
            self.assertFalse(await self.worker.maintenance.erase(record))
        erasure = self.queue.control.erasures("test")[0]
        self.assertEqual(erasure["phase"], "pending")
        self.assertEqual(erasure["cleanup_step"], 0)
        self.assertEqual(erasure["next_at"], 0)
        self.assertNotIn("absent-128", reads)
        await self.finish_cleanup()

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
