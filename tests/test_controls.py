"""Crash, restore, and in-flight races that a chat smoke test cannot reproduce."""

import asyncio
import json
import logging
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
import test_reliability as reliability

from plugins.astrbot_plugin_hindsight_bridge.core.client import HindsightClient
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Scope
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox, QueueError
from plugins.astrbot_plugin_hindsight_bridge.core.permissions import migrate, pairs
from plugins.astrbot_plugin_hindsight_bridge.core.worker import Worker


@contextmanager
def database(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


class MemoryService(reliability.Service):
    def __init__(self):
        super().__init__()
        self.op_banks, self.documents, self.cancelled, self.deleted = {}, {}, [], []
        self.lose_delete_ack = False
        self.admit_gate = None
        self.submit_started = asyncio.Event()
        self.conflict_once = False

    async def __call__(self, request):
        path, method = request.url.path, request.method
        if request.headers.get("Authorization") != "Bearer valid-key":
            return httpx.Response(401, json={})
        if path == "/v1/default/banks":
            self.requests.append((method, path))
            return httpx.Response(
                200,
                json={
                    "banks": [
                        {"bank_id": bank}
                        for bank in self.banks
                        if request.url.params.get("q", "") in bank
                    ]
                },
            )
        bank = path.split("/")[4]
        if path.endswith("/operations"):
            rows = [
                {**op, "id": oid, "operation_id": None}
                for oid, op in self.operations.items()
                if self.op_banks.get(oid) == bank
            ]
            offset = int(request.url.params.get("offset", 0))
            limit = int(request.url.params.get("limit", 100))
            self.requests.append((method, path + "?" + str(request.url.query)))
            return httpx.Response(
                200 if bank in self.banks else 404,
                json={"operations": rows[offset : offset + limit], "total": len(rows)},
            )
        if method == "DELETE" and "/operations/" in path:
            oid = path.split("/")[-1]
            self.cancelled.append(oid)
            if oid not in self.operations:
                return httpx.Response(404, json={})
            status = self.operations[oid]["status"]
            if self.conflict_once:
                self.conflict_once = False
                self.operations[oid]["status"] = "completed"
                return httpx.Response(409, json={})
            if status not in {"pending", "processing"}:
                return httpx.Response(409, json={})
            self.operations[oid]["status"] = "cancelled"
            for op in self.operations.values():
                for child in op.get("child_operations") or []:
                    if child["operation_id"] == oid:
                        child["status"] = "cancelled"
            return httpx.Response(200, json={"success": True})
        if method == "DELETE" and path == "/v1/default/banks/" + bank:
            self.banks.discard(bank)
            self.deleted.append(bank)
            for oid in list(self.operations):
                if self.op_banks.get(oid) == bank:
                    self.operations.pop(oid)
            self.documents.pop(bank, None)
            if self.lose_delete_ack:
                self.lose_delete_ack = False
                raise httpx.ReadError("delete_ack_lost", request=request)
            return httpx.Response(200, json={"success": True})
        if "/documents/" in path and method == "GET":
            doc = path.split("/documents/")[1]
            return httpx.Response(
                200 if doc in self.documents.get(bank, set()) else 404, json={}
            )
        if method == "POST" and path.endswith("/memories"):
            self.submit_started.set()
            if self.admit_gate:
                await self.admit_gate.wait()
            body = json.loads(request.content)
            try:
                return await super().__call__(request)
            finally:
                oid = body["operation_id"]
                if oid in self.operations:
                    self.banks.add(bank)
                    self.op_banks[oid] = bank
                    self.documents.setdefault(bank, set()).add(
                        body["items"][0]["document_id"]
                    )
        return await super().__call__(request)

    def add_child(self, bank, root, child, status="processing"):
        self.operations[root].setdefault("child_operations", [])
        if self.operations[root]["child_operations"] is None:
            self.operations[root]["child_operations"] = []
        self.operations[root]["child_operations"].append(
            {"operation_id": child, "status": status}
        )
        self.operations[child] = {"operation_id": child, "status": status}
        self.op_banks[child] = bank


class Controls(unittest.IsolatedAsyncioTestCase):
    open = reliability.DurableCore.open
    incoming = reliability.DurableCore.incoming
    enqueue = reliability.DurableCore.enqueue
    immediately = reliability.DurableCore.immediately
    asyncTearDown = reliability.DurableCore.asyncTearDown

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / "runtime"
        self.service = MemoryService()
        self.client = HindsightClient(
            "http://memory.test",
            "valid-key",
            transport=httpx.MockTransport(self.service),
        )
        self.open()
        self.logger = logging.getLogger("hindsight-controls")
        self.logger.addHandler(logging.NullHandler())
        self.worker = Worker(self.queue, self.client, self.logger, poll_interval=0.01)

    async def test_pause_freezes_ready_and_readonly_tracks_accepted_then_resumes(self):
        first = self.enqueue()
        original = dict(self.queue.get(first.task_id))
        old = self.queue.scope_owner(first.scope_id)
        self.queue.set_mode(first.scope_id, "paused")
        self.assertFalse(self.queue.can_recall(old))
        self.assertFalse(await self.worker.step())
        self.assertEqual(self.service.submissions, [])
        self.assertTrue(
            self.queue.can_recall(self.queue.scope_owner(first.scope_id), manual=True)
        )
        with self.assertRaisesRegex(QueueError, "scope_not_active"):
            self.enqueue(self.incoming(mid="while-paused"))
        self.queue.set_mode(first.scope_id, "active", now=time.time() - 1)
        self.service.pending = True
        await self.worker.step()
        self.queue.set_mode(first.scope_id, "readonly")
        readonly = self.queue.scope_owner(first.scope_id)
        self.assertTrue(self.queue.can_recall(readonly))
        with self.assertRaisesRegex(QueueError, "scope_not_active"):
            self.enqueue(self.incoming(mid="while-readonly"))
        oid = original["operation_id"]
        self.service.operations[oid]["status"] = "completed"
        self.immediately()
        await self.worker.step()
        self.assertEqual(self.queue.get(first.task_id)["state"], "done")
        self.assertEqual(self.service.submissions[0]["operation_id"], oid)
        self.assertEqual(
            self.queue.db.execute("SELECT COUNT(*) FROM window").fetchone()[0], 0
        )
        self.queue.set_mode(first.scope_id, "active", now=time.time() + 2)
        with self.assertRaisesRegex(QueueError, "message_before_accept_from"):
            self.enqueue(self.incoming(mid="replayed-disabled-message"))

    async def test_paused_failure_does_not_retry_even_with_older_retry_intent(self):
        task = self.enqueue()
        oid = self.queue.get(task.task_id)["operation_id"]
        self.service.tree(oid, "failed", "failed")
        await self.worker.step()
        self.queue.request_retry(task.scope_id)
        self.queue.set_mode(task.scope_id, "paused")
        self.assertFalse(await self.worker.step())
        self.assertEqual(self.service.retries, [])

    async def test_cancel_never_submits_ready_and_tracks_late_children(self):
        task = self.enqueue()
        self.queue.request_cancel(task.scope_id, task.task_id)
        self.assertEqual(self.queue.get(task.task_id)["payload"], "")
        self.immediately()
        await self.worker.step()
        self.assertEqual(self.service.submissions, [])
        other = self.enqueue(self.incoming(mid="other"))
        self.service.pending = True
        await self.worker.step()
        oid = self.queue.get(other.task_id)["operation_id"]
        self.service.add_child(other.bank, oid, "child-before-cancel")
        self.queue.request_cancel(other.scope_id, other.task_id)
        await self.worker.step()
        self.assertEqual(self.queue.get(other.task_id)["state"], "cancelled")
        self.assertIn("child-before-cancel", self.service.cancelled)
        self.service.add_child(other.bank, oid, "late-child")
        with self.queue.db:
            self.queue.db.execute(
                "UPDATE outbox SET next_at=0 WHERE id=?", (other.task_id,)
            )
        await self.worker.step()
        self.assertIn("late-child", self.service.cancelled)
        self.assertEqual(self.queue.request_retry(other.scope_id), 0)

    async def test_cancel_target_is_owner_scoped_and_409_is_read_again(self):
        alice = self.enqueue()
        bob = self.enqueue(self.incoming(user="bob"))
        with self.assertRaisesRegex(QueueError, "outside_memory_scope"):
            self.queue.request_cancel(alice.scope_id, bob.task_id)
        self.service.pending = True
        await self.worker.step()
        self.queue.request_cancel(alice.scope_id, alice.task_id)
        self.service.conflict_once = True
        self.immediately()
        await self.worker.step()
        self.assertEqual(self.queue.get(alice.task_id)["state"], "cancelled")
        self.assertEqual(self.service.retries, [])

    async def test_forget_cancels_all_pages_and_removes_operation_payloads(self):
        task = self.enqueue()
        self.service.pending = True
        await self.worker.step()
        oid = self.queue.get(task.task_id)["operation_id"]
        self.service.add_child(task.bank, oid, "retain-child")
        for index in range(105):
            key = f"consolidation-{index}"
            self.service.operations[key] = {"operation_id": key, "status": "pending"}
            self.service.op_banks[key] = task.bank
        stale_snapshot = self.queue.scope_owner(task.scope_id)
        record = self.queue.forget(task.scope_id)
        self.assertFalse(self.queue.can_recall(stale_snapshot, manual=True))
        self.assertTrue(await self.worker.maintenance.check(force=True))
        self.assertEqual(len(set(self.service.cancelled)), 107)
        self.assertNotIn(task.bank, self.service.banks)
        self.assertEqual(self.queue.get(task.task_id)["state"], "discarded")
        self.assertEqual(self.queue.get(task.task_id)["payload"], "")
        self.assertEqual(self.queue.scope_owner(task.scope_id)["mode"], "paused")
        self.assertNotEqual(record["new_bank"], task.bank)
        control_dump = "\n".join(self.queue.control.db.iterdump())
        self.assertNotIn("我喝咖啡", control_dump)

    async def test_late_submit_ack_cannot_resurrect_discarded_task(self):
        task = self.enqueue()
        self.service.admit_gate = asyncio.Event()
        running = asyncio.create_task(self.worker.step())
        await self.service.submit_started.wait()
        self.queue.forget(task.scope_id)
        self.assertTrue(await self.worker.maintenance.check(force=True))
        self.service.admit_gate.set()
        await running
        self.assertIn(task.bank, self.service.banks)
        self.assertEqual(self.queue.get(task.task_id)["state"], "discarded")
        self.assertTrue(await self.worker.maintenance.check())
        self.assertNotIn(task.bank, self.service.banks)
        self.assertEqual(len(self.service.submissions), 1)

    async def test_control_commit_survives_crash_before_runtime_commit(self):
        task = self.enqueue()
        with (
            patch.object(
                self.queue,
                "apply_control",
                side_effect=sqlite3.OperationalError("injected"),
            ),
            self.assertRaises(sqlite3.OperationalError),
        ):
            self.queue.forget(task.scope_id)
        self.assertFalse(self.queue.current(self.queue.get(task.task_id)))
        self.queue.close()
        self.open()
        self.assertTrue(self.queue.recovering)
        self.worker = Worker(self.queue, self.client, self.logger)
        await self.worker.step()
        self.assertFalse(self.queue.recovering)
        self.assertEqual(self.queue.get(task.task_id)["state"], "discarded")
        self.assertEqual(self.service.submissions, [])

    async def test_delete_response_loss_continues_idempotently(self):
        task = self.enqueue()
        await self.worker.step()
        self.queue.forget(task.scope_id)
        self.service.lose_delete_ack = True
        self.assertFalse(await self.worker.maintenance.check(force=True))
        self.assertTrue(await self.worker.maintenance.check(force=True))
        self.assertEqual(self.service.deleted.count(task.bank), 2)
        self.assertEqual(
            self.queue.control.erasures("test-service")[0]["phase"], "verified"
        )

    async def test_old_business_backup_replays_new_control_and_rejects_old_events(self):
        task = self.enqueue()
        backup = Path(self.tmp.name) / "monday.sqlite3"
        with database(backup) as destination:
            self.queue.db.backup(destination)
        record = self.queue.forget(task.scope_id, now=time.time() + 3)
        self.assertTrue(await self.worker.maintenance.check(force=True))
        deployment = self.queue.deployment_id
        self.queue.close()
        # SQLite backup restores business data while leaving the control store untouched.
        with (
            database(backup) as source,
            database(self.directory / "runtime.sqlite3") as target,
        ):
            source.backup(target)
        self.open()
        self.assertTrue(self.queue.recovering)
        self.worker = Worker(self.queue, self.client, self.logger)
        await self.worker.step()
        scope = self.queue.scope_owner(task.scope_id)
        self.assertEqual(scope["bank"], record["new_bank"])
        self.assertEqual(scope["mode"], "paused")
        self.assertEqual(self.queue.deployment_id, deployment)
        self.assertEqual(self.queue.get(task.task_id)["payload"], "")
        self.queue.set_mode(task.scope_id, "active", now=time.time() + 4)
        with self.assertRaisesRegex(QueueError, "message_before_accept_from"):
            self.enqueue(self.incoming(mid="late-unseen-old-event"))
        self.assertEqual(self.service.submissions, [])

    async def test_accepted_ledger_prevents_replay_after_restoring_unsubmitted_snapshot(
        self,
    ):
        task = self.enqueue()
        backup = Path(self.tmp.name) / "ready.sqlite3"
        with database(backup) as destination:
            self.queue.db.backup(destination)
        await self.worker.step()
        self.queue.close()
        self.service.operations.clear()
        with (
            database(backup) as source,
            database(self.directory / "runtime.sqlite3") as target,
        ):
            source.backup(target)
        self.open()
        self.worker = Worker(self.queue, self.client, self.logger)
        await self.worker.step()
        self.assertEqual(self.queue.get(task.task_id)["state"], "reconcile_required")
        self.assertEqual(len(self.service.submissions), 1)

    async def test_missing_or_rolled_back_control_cannot_initialize_new_identity(self):
        task = self.enqueue()
        control_path = self.queue.control.directory
        older_control = Path(self.tmp.name) / "old-control.sqlite3"
        with database(older_control) as dest:
            self.queue.control.db.backup(dest)
        self.queue.forget(task.scope_id)
        self.queue.close()
        with self.assertRaisesRegex(QueueError, "control_missing"):
            self.open(control_directory=Path(self.tmp.name) / "missing")
        with (
            database(older_control) as source,
            database(control_path / "control.sqlite3") as target,
        ):
            source.backup(target)
        with self.assertRaisesRegex(QueueError, "control_rolled_back"):
            self.open()

    async def test_principal_binding_survives_restart_and_keeps_source_permissions(
        self,
    ):
        task = self.enqueue()
        alice = self.queue.scope_owner(task.scope_id)
        second = replace(
            self.incoming(), platform_instance_id="instance-2", user_id="other-account"
        )
        self.queue.control.attach_verified_account(
            "test-service", second.scope(self.queue.deployment_id), alice["owner_id"]
        )
        self.queue.sync_permissions([("instance-1", "alice")])
        linked = self.queue.scope(second)
        self.assertEqual(linked["bank"], task.bank)
        self.assertEqual(linked["principal_id"], alice["principal_id"])
        self.assertFalse(linked["allowed"])
        self.queue.sync_permissions(
            [("instance-1", "alice"), ("instance-2", "other-account")]
        )
        linked_task = self.enqueue(second)
        self.service.pending = True
        await self.worker.step()
        self.immediately()
        await self.worker.step()
        self.assertEqual(len(self.service.submissions), 1)
        self.queue.set_mode(task.scope_id, "readonly")
        self.assertEqual(
            self.queue.scope_owner(linked_task.scope_id)["mode"], "readonly"
        )
        self.queue.close()
        self.open()
        self.assertEqual(
            self.queue.scope(second)["principal_id"], alice["principal_id"]
        )
        self.assertEqual(self.queue.scope(second)["bank"], task.bank)
        with self.assertRaisesRegex(QueueError, "explicit_migration"):
            bob = self.queue.scope(self.incoming(user="bob"))
            self.queue.control.attach_verified_account(
                "test-service", second.scope(self.queue.deployment_id), bob["owner_id"]
            )


class PermissionMigration(unittest.TestCase):
    def test_exact_pairs_and_legacy_ambiguity(self):
        config = {
            "allowed_platform_ids": ["A"],
            "allowed_user_ids": ["X", "Y"],
        }
        selected, error = migrate(config)
        self.assertEqual(selected, {("A", "X"), ("A", "Y")})
        self.assertFalse(error)
        self.assertNotIn("allowed_user_ids", config)
        ambiguous = {"allowed_platform_ids": ["A", "B"], "allowed_user_ids": ["X", "Y"]}
        self.assertEqual(migrate(ambiguous), (frozenset(), "permission_pairs_required"))
        configured = {
            "permission_config_version": 2,
            "allowed_accounts": [
                {"platform_instance_id": "A", "user_id": "X"},
                {"platform_instance_id": "B", "user_id": "Y"},
            ],
            "allowed_user_ids": ["X", "Y"],
            "allowed_platform_ids": ["A", "B"],
        }
        selected, _ = migrate(configured)
        self.assertNotIn(("A", "Y"), selected)
        self.assertNotIn(("B", "X"), selected)
        self.assertEqual(
            migrate({"allowed_user_ids": ["X"]}, [("observed", "X")])[0],
            {("observed", "X")},
        )
        with self.assertRaises(QueueError):
            pairs([{"platform_instance_id": "", "user_id": "X"}])

    def test_p1_schema_migration_preserves_bank_and_new_principal(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "runtime"
            directory.mkdir()
            source = Scope("fixed-deployment", "aiocqhttp", "A", "bot", "X")
            with database(directory / "runtime.sqlite3") as db:
                db.executescript("""
                    CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE connections(id TEXT PRIMARY KEY,url TEXT NOT NULL,
                        address_revision INTEGER NOT NULL,credential_fingerprint TEXT NOT NULL,
                        credential_revision INTEGER NOT NULL,paused_error TEXT NOT NULL DEFAULT '');
                    CREATE TABLE scopes(connection_id TEXT NOT NULL,scope_id TEXT NOT NULL,
                        identity_json TEXT NOT NULL,platform_instance_id TEXT NOT NULL,user_id TEXT NOT NULL,
                        bank TEXT NOT NULL,generation INTEGER NOT NULL DEFAULT 0,
                        allowed INTEGER NOT NULL DEFAULT 1,mode TEXT NOT NULL DEFAULT 'active',
                        accept_from INTEGER NOT NULL,PRIMARY KEY(connection_id,scope_id));
                    INSERT INTO settings VALUES ('deployment_id','fixed-deployment');
                    INSERT INTO settings VALUES ('accept_from','1');
                    PRAGMA user_version=1;
                """)
                db.execute(
                    "INSERT INTO connections VALUES ('s','http://memory.test',1,'key',1,'')"
                )
                db.execute(
                    "INSERT INTO scopes VALUES (?,?,?,?,?,?,0,1,'active',1)",
                    ("s", source.id, source.identity, "A", "X", source.bank()),
                )
            queue = Outbox(
                directory, "s", "http://memory.test", credential_fingerprint="key"
            )
            try:
                self.assertEqual(queue.deployment_id, "fixed-deployment")
                self.assertEqual(queue.scope_owner(source.id)["bank"], source.bank())
                principal = queue.scope_owner(source.id)["principal_id"]
                self.assertTrue(principal)
            finally:
                queue.close()
            queue = Outbox(
                directory, "s", "http://memory.test", credential_fingerprint="key"
            )
            try:
                self.assertEqual(
                    queue.scope_owner(source.id)["principal_id"], principal
                )
                self.assertEqual(queue.scope_owner(source.id)["bank"], source.bank())
            finally:
                queue.close()
