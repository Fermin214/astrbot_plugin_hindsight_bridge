"""Fault cases that are impractical to reproduce reliably in a live QQ chat."""

import asyncio
import json
import logging
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from plugins.astrbot_plugin_hindsight_bridge.client import HindsightClient
from plugins.astrbot_plugin_hindsight_bridge.identity import (
    Incoming,
    Scope,
    canonical,
    sha256,
)
from plugins.astrbot_plugin_hindsight_bridge.materials import (
    memory_block,
    recall_query,
    token_count,
)
from plugins.astrbot_plugin_hindsight_bridge.outbox import Outbox, QueueError
from plugins.astrbot_plugin_hindsight_bridge.source import local_runner, native_snapshot
from plugins.astrbot_plugin_hindsight_bridge.worker import Worker


class Service:
    """A controlled public HTTP boundary, with explicit faults and task states."""

    def __init__(self):
        self.banks, self.operations = set(), {}
        self.submissions, self.retries, self.requests = [], [], []
        self.lose_ack = self.fail_before_accept = False
        self.authorized, self.pending = True, False
        self.rate_limit = False

    async def __call__(self, request):
        path = request.url.path
        self.requests.append((request.method, path))
        key = request.headers.get("Authorization")
        if key != "Bearer valid-key" or not self.authorized:
            return httpx.Response(401, json={})
        if self.rate_limit:
            return httpx.Response(429, json={}, headers={"Retry-After": "17"})
        if path == "/v1/default/banks":
            return httpx.Response(200, json={"banks": []})
        bank = path.split("/")[4]
        if path.endswith("/config"):
            return httpx.Response(200 if bank in self.banks else 404, json={})
        if request.method == "PUT":
            self.banks.add(bank)
            return httpx.Response(200, json={})
        if "/operations/" in path:
            if path.endswith("/retry"):
                oid = path.split("/")[-2]
                self.retries.append(oid)
                self.operations[oid]["status"] = "completed"
                for child in self.operations[oid].get("child_operations", []):
                    child["status"] = "completed"
                    self.operations[child["operation_id"]]["status"] = "completed"
                return httpx.Response(200, json={})
            oid = path.split("/")[-1]
            return httpx.Response(
                200,
                json=self.operations.get(
                    oid,
                    {
                        "operation_id": oid,
                        "status": "not_found",
                        "child_operations": None,
                    },
                ),
            )
        if path.endswith("/memories") and request.method == "POST":
            body = json.loads(request.content)
            self.submissions.append(body)
            if self.fail_before_accept:
                self.fail_before_accept = False
                raise httpx.ConnectError("injected", request=request)
            oid = body["operation_id"]
            self.operations.setdefault(
                oid,
                {
                    "operation_id": oid,
                    "status": "processing" if self.pending else "completed",
                    "child_operations": None,
                },
            )
            if self.lose_ack:
                self.lose_ack = False
                raise httpx.ReadError("injected", request=request)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "async": True,
                    "operation_id": oid,
                },
            )
        if path.endswith("/recall"):
            return httpx.Response(200, json={"results": []})
        raise AssertionError((request.method, path))

    def tree(self, root, root_status, *statuses):
        children = [
            {"operation_id": f"child-{index}", "status": status}
            for index, status in enumerate(statuses)
        ]
        self.operations[root] = {
            "operation_id": root,
            "status": root_status,
            "child_operations": children,
        }
        for child in children:
            self.operations[child["operation_id"]] = dict(child)


class DurableCore(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / "runtime"
        self.service = Service()
        self.client = HindsightClient(
            "http://memory.test",
            "valid-key",
            transport=httpx.MockTransport(self.service),
        )
        self.open()
        self.logger = logging.getLogger("hindsight-test")
        self.logger.addHandler(logging.NullHandler())
        self.worker = Worker(self.queue, self.client, self.logger, poll_interval=0.01)

    def open(self, **kwargs):
        self.queue = Outbox(
            self.directory,
            "test-service",
            "http://memory.test",
            credential_fingerprint=sha256("valid-key"),
            zone="Asia/Hong_Kong",
            **kwargs,
        )

    async def asyncTearDown(self):
        await self.client.close()
        self.queue.close()
        self.tmp.cleanup()

    def incoming(self, text="我喝咖啡不要糖。", user="alice", mid="100"):
        return Incoming(
            "aiocqhttp", "instance-1", "bot-1", user, mid, int(time.time()), text
        )

    def enqueue(self, incoming=None, conversation="conversation-1", release=True):
        capture = self.queue.capture(incoming or self.incoming(), conversation)
        if release:
            self.queue.release(capture.task_id)
        return capture

    def immediately(self):
        with self.queue.db:
            self.queue.db.execute("UPDATE outbox SET next_at=0")

    async def test_lost_ack_survives_reopen_with_one_original_operation(self):
        capture = self.enqueue()
        frozen = dict(self.queue.get(capture.task_id))
        self.service.lose_ack = True
        await self.worker.step()
        self.assertEqual(self.queue.get(capture.task_id)["state"], "unknown")
        deployment = self.queue.deployment_id
        self.queue.close()
        self.open()
        self.worker.outbox = self.queue
        self.immediately()
        await self.worker.step()
        row = self.queue.get(capture.task_id)
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["operation_id"], frozen["operation_id"])
        self.assertEqual(row["payload_sha256"], frozen["payload_sha256"])
        self.assertEqual(self.queue.deployment_id, deployment)
        self.assertEqual(row["payload"], "")
        self.assertEqual(len(self.service.submissions), 1)

    async def test_unaccepted_unknown_replays_identical_frozen_body(self):
        capture = self.enqueue()
        self.service.fail_before_accept = True
        await self.worker.step()
        self.immediately()
        await self.worker.step()
        self.assertEqual(len(self.service.submissions), 2)
        self.assertEqual(self.service.submissions[0], self.service.submissions[1])
        self.assertEqual(self.queue.get(capture.task_id)["state"], "tracking")

    async def test_accepted_history_missing_requires_reconciliation(self):
        capture = self.enqueue()
        await self.worker.step()
        self.service.operations.clear()
        self.immediately()
        await self.worker.step()
        self.assertEqual(self.queue.get(capture.task_id)["state"], "reconcile_required")
        self.assertEqual(len(self.service.submissions), 1)

    async def test_complete_parent_waits_for_known_child(self):
        capture = self.enqueue()
        row = self.queue.get(capture.task_id)
        self.service.tree(row["operation_id"], "completed", "processing")
        await self.worker.step()
        self.assertEqual(self.queue.get(capture.task_id)["state"], "tracking")
        self.service.operations[row["operation_id"]].pop("child_operations")
        self.service.operations["child-0"]["status"] = "completed"
        self.immediately()
        await self.worker.step()
        self.assertEqual(self.queue.get(capture.task_id)["state"], "done")
        self.assertEqual(len(self.queue.operations(capture.task_id)), 2)

    async def test_frozen_context_tamper_prevents_all_network_calls(self):
        capture = self.enqueue()
        row = self.queue.get(capture.task_id)
        payload = json.loads(row["payload"])
        payload["items"][0]["context"] += " changed"
        with self.queue.db:
            self.queue.db.execute(
                "UPDATE outbox SET payload=? WHERE id=?",
                (canonical(payload), capture.task_id),
            )
        await self.worker.step()
        self.assertEqual(
            self.queue.get(capture.task_id)["last_error"], "payload_digest_conflict"
        )
        self.assertEqual(self.service.requests, [])
        self.assertEqual(self.queue.request_retry(capture.scope_id), 0)

    async def test_failed_uses_retry_endpoint_and_preserves_cancelled_sibling(self):
        capture = self.enqueue()
        oid = self.queue.get(capture.task_id)["operation_id"]
        self.service.tree(oid, "failed", "failed", "cancelled")
        await self.worker.step()
        self.assertEqual(self.queue.get(capture.task_id)["state"], "failed")
        self.assertEqual(self.service.retries, [])
        self.queue.request_retry(capture.scope_id)
        await self.worker.step()
        self.assertEqual(self.service.retries, ["child-0"])
        self.assertEqual(self.service.operations["child-1"]["status"], "cancelled")
        self.assertEqual(self.service.submissions, [])

    async def test_failed_tree_without_cancellation_retries_root(self):
        capture = self.enqueue()
        oid = self.queue.get(capture.task_id)["operation_id"]
        self.service.tree(oid, "failed", "failed")
        await self.worker.step()
        self.queue.request_retry(capture.scope_id)
        await self.worker.step()
        self.assertEqual(self.service.retries, [oid])
        self.immediately()
        await self.worker.step()
        self.assertEqual(self.queue.get(capture.task_id)["state"], "done")

    async def test_remote_concurrency_is_two_and_one_per_scope(self):
        self.service.pending = True
        captures = [
            self.enqueue(self.incoming(user="alice", mid="1")),
            self.enqueue(self.incoming(user="alice", mid="2")),
            self.enqueue(self.incoming(user="bob", mid="1")),
            self.enqueue(self.incoming(user="carol", mid="1")),
        ]
        running = asyncio.create_task(self.worker.run())
        try:
            async with asyncio.timeout(2):
                while len(self.service.submissions) < 2:
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.08)
            self.assertEqual(len(self.service.submissions), 2)
            submitted_scopes = {
                p["items"][0]["metadata"]["scope_id"] for p in self.service.submissions
            }
            self.assertEqual(
                submitted_scopes, {captures[0].scope_id, captures[2].scope_id}
            )
            first_id = self.service.submissions[0]["operation_id"]
            self.service.operations[first_id]["status"] = "completed"
            async with asyncio.timeout(2):
                while len(self.service.submissions) < 3:
                    await asyncio.sleep(0.01)
            self.assertEqual(len(self.service.submissions), 3)
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_authentication_pause_and_retry_after(self):
        capture = self.enqueue()
        self.service.authorized = False
        await self.worker.step()
        self.assertEqual(self.queue.connection_error(), "http_401")
        self.assertIsNone(self.queue.due())
        self.service.authorized = True
        await self.client.ensure_auth(force=True)
        self.queue.pause_connection("")
        self.client.auth_until = 0
        self.service.rate_limit = True
        self.immediately()
        before = time.time()
        await self.worker.step()
        self.assertGreaterEqual(self.queue.get(capture.task_id)["next_at"], before + 17)
        self.assertEqual(self.service.submissions, [])

    async def test_retry_does_not_overlap_later_operation_in_same_scope(self):
        first = self.enqueue(self.incoming(mid="first"))
        first_id = self.queue.get(first.task_id)["operation_id"]
        self.service.tree(first_id, "failed")
        await self.worker.step()
        self.service.pending = True
        second = self.enqueue(self.incoming(mid="second"))
        await self.worker.step()
        self.queue.request_retry(first.scope_id)
        self.immediately()
        await self.worker.step()
        self.assertEqual(self.service.retries, [])
        second_id = self.queue.get(second.task_id)["operation_id"]
        self.service.operations[second_id]["status"] = "completed"
        self.immediately()
        await self.worker.step()
        await self.worker.step()
        self.assertEqual(self.service.retries, [first_id])

    async def test_removed_permission_tracks_accepted_work_without_new_writes(self):
        capture = self.enqueue()
        self.service.pending = True
        await self.worker.step()
        self.queue.sync_permissions([])
        self.immediately()
        await self.worker.step()
        self.assertEqual(len(self.service.submissions), 1)
        oid = self.queue.get(capture.task_id)["operation_id"]
        self.service.operations[oid]["status"] = "completed"
        self.immediately()
        await self.worker.step()
        self.assertEqual(self.queue.get(capture.task_id)["state"], "done")
        self.assertFalse(self.queue.current(self.queue.get(capture.task_id)))

    async def test_atomic_freeze_duplicate_conflict_and_capacity(self):
        original = self.incoming("周五上午去看展。", mid="first")
        first = self.enqueue(original, release=False)
        frozen = dict(self.queue.get(first.task_id))
        self.assertIsNone(self.queue.due())
        second = self.enqueue(
            replace(original, message_id="second", text="改成下午吧。")
        )
        body = json.loads(self.queue.get(second.task_id)["payload"])
        self.assertIn(original.text, body["items"][0]["content"])
        self.assertEqual(body["items"][0]["observation_scopes"], "shared")
        self.assertTrue(
            all(isinstance(v, str) for v in body["items"][0]["metadata"].values())
        )
        self.queue.max_tasks = 2
        self.assertFalse(self.queue.capture(original, "new-conversation").created)
        with self.assertRaisesRegex(QueueError, "message_identity_content_conflict"):
            self.queue.capture(replace(original, text="我改了原文"), "conversation-1")
        with self.assertRaisesRegex(QueueError, "queue_capacity_reached"):
            self.queue.capture(replace(original, message_id="third"), "conversation-1")
        self.assertEqual(
            self.queue.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2
        )
        self.assertEqual(self.queue.get(first.task_id)["payload"], frozen["payload"])
        self.queue.close()
        self.open()
        self.assertEqual(self.queue.get(first.task_id)["released"], 1)
        self.assertEqual(self.queue.get(first.task_id)["payload"], frozen["payload"])

    async def test_disk_write_failure_rolls_back_event_window_and_task(self):
        original = self.queue.db.execute("PRAGMA query_only").fetchone()[0]
        self.queue.scope(self.incoming())
        self.queue.db.execute("PRAGMA query_only=ON")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.queue.capture(self.incoming(), "conversation-1")
        finally:
            self.queue.db.execute(f"PRAGMA query_only={original}")
        for table in ("outbox", "events", "window"):
            self.assertEqual(
                self.queue.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
            )

    async def test_service_switch_leaves_queue_and_move_is_explicit(self):
        capture = self.enqueue()
        self.queue.close()
        with self.assertRaisesRegex(QueueError, "service_address_revision_required"):
            Outbox(
                self.directory,
                "test-service",
                "http://other.test",
                credential_fingerprint="x",
            )
        self.queue = Outbox(
            self.directory,
            "other-service",
            "http://other.test",
            credential_fingerprint="x",
        )
        self.assertIsNone(self.queue.due())
        self.assertEqual(self.queue.get(capture.task_id)["state"], "ready")
        self.queue.close()
        self.queue = Outbox(
            self.directory,
            "test-service",
            "http://moved.test",
            address_revision=2,
            credential_fingerprint=sha256("valid-key"),
        )
        self.assertEqual(self.queue.due()["id"], capture.task_id)

    async def test_process_lock_and_stable_cross_conversation_isolation(self):
        first = self.enqueue(self.incoming(), conversation="old")
        second = self.enqueue(self.incoming(mid="2"), conversation="new")
        third = self.enqueue(self.incoming(user="bob"), conversation="old")
        self.assertEqual(first.bank, second.bank)
        self.assertEqual(second.history, ())
        self.assertNotEqual(first.bank, third.bank)
        self.assertEqual(third.history, ())
        with self.assertRaises(OSError):
            self.open()


class SourceAndQuery(unittest.TestCase):
    def test_identity_has_all_stable_fields_and_no_conversation(self):
        scope = Scope("deployment", "aiocqhttp", "platform", "bot", "user")
        expected = sha256(
            canonical(["deployment", "aiocqhttp", "platform", "bot", "user", "default"])
        )
        self.assertEqual(scope.id, expected)
        for key in (
            "deployment_id",
            "platform_instance_id",
            "bot_id",
            "user_id",
            "memory_space_id",
        ):
            self.assertNotEqual(
                scope.bank(), replace(scope, **{key: "different"}).bank()
            )

    def test_native_source_gate_and_internal_text_exclusions(self):
        class Plain:
            def __init__(self, text):
                self.text = text

        text = "  我喝咖啡不要糖。  "
        raw = {
            "post_type": "message",
            "message_type": "private",
            "message_id": 123,
            "user_id": 1,
            "self_id": 2,
            "time": int(time.time()),
            "message": [{"type": "text", "data": {"text": text}}],
        }
        event = SimpleNamespace(
            get_platform_name=lambda: "aiocqhttp",
            is_private_chat=lambda: True,
            get_group_id=lambda: "",
            get_platform_id=lambda: "onebot",
            get_self_id=lambda: "2",
            get_sender_id=lambda: "1",
            get_messages=lambda: [Plain(text)],
            get_message_str=lambda: text.strip(),
            message_obj=SimpleNamespace(message_id="123", raw_message=raw),
        )
        self.assertEqual(native_snapshot(event, Plain).text, text)
        for key, value in (
            ("post_type", "notice"),
            ("message_type", "group"),
            ("self_id", 3),
            ("user_id", 9),
            ("message_id", 124),
            ("time", None),
            ("message", [{"type": "reply", "data": {"id": "5"}}]),
        ):
            with patch.dict(raw, {key: value}):
                self.assertIsNone(native_snapshot(event, Plain))
        event.message_obj.raw_message = None
        self.assertIsNone(native_snapshot(event, Plain))
        self.assertFalse(local_runner({"agent_runner": {"runner_type": "dify"}}))
        self.assertTrue(local_runner({"agent_runner": {"runner_type": "local"}}))

    def test_unicode_queries_and_memory_injection_have_hard_budgets(self):
        question = "我现在最关心的偏好是🧠" * 800 + "请保留这个末尾"
        query = recall_query(question, [("用户", "<|endoftext|>咖啡" * 400)])
        self.assertLessEqual(token_count(query), 380)
        self.assertIn("请保留这个末尾", query)
        short = recall_query("上次那个呢？", [("用户", "我喝咖啡不要糖。")])
        self.assertIn("咖啡", short)
        block = memory_block(
            [
                {"text": "偏好" * 4000},
                {
                    "text": "用户不加糖",
                    "type": "world",
                    "document_id": "doc",
                    "occurred_start": "2026-10-24",
                },
            ]
        )
        self.assertLessEqual(len(block), 4000)
        self.assertIn("doc", block)
        self.assertIn("2026-10-24", block)
