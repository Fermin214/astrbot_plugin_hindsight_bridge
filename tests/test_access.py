"""Authorization, replay and historical management contracts without a platform."""

from dataclasses import replace
import asyncio
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from plugins.astrbot_plugin_hindsight_bridge.core import onebot_source
from plugins.astrbot_plugin_hindsight_bridge.core.client import Operation
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming
from plugins.astrbot_plugin_hindsight_bridge.core.maintenance import Maintenance
from plugins.astrbot_plugin_hindsight_bridge.core.materials import Message, retain_payload
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox, QueueError
from plugins.astrbot_plugin_hindsight_bridge.core.source import (
    native_header, platform_details, valid_user_id,
)
from plugins.astrbot_plugin_hindsight_bridge.core.worker import Worker


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = patch.object(time, "time", return_value=100.25)
        self.clock.start()
        self.open()
        self.incoming = Incoming(
            "aiocqhttp", "onebot", "1001", "2001", "message", 101, "hello", received_at=100.5,
        )

    def open(self):
        self.queue = Outbox(
            self.root / "runtime", "service", "http://memory.test", credential_fingerprint="test",
        )

    def tearDown(self):
        self.queue.close()
        self.clock.stop()
        self.tmp.cleanup()

    def authorize(self, incoming=None):
        incoming = incoming or self.incoming
        self.queue.sync_permissions([(incoming.platform_instance_id, incoming.user_id)])
        row = self.queue.access.authorization(incoming.platform_instance_id, incoming.user_id)
        return self.queue.activate(incoming, row["revision"])

    def test_selection_requires_explicit_activation_and_old_config_does_not_reactivate(self):
        pair = [("onebot", "2001")]
        self.assertFalse(self.queue.accepts(self.incoming))

        with self.assertRaisesRegex(QueueError, "permissions_not_synced"):
            self.queue.capture(self.incoming, "conversation")
        self.queue.sync_permissions([])
        self.assertFalse(self.queue.accepts(self.incoming))
        with self.assertRaisesRegex(QueueError, "scope_not_active"):
            self.queue.capture(self.incoming, "conversation")
        self.queue.sync_permissions(pair)
        self.assertFalse(self.queue.accepts(self.incoming))
        self.authorize()
        self.assertTrue(self.queue.accepts(self.incoming))
        first = self.queue.access.authorization(*pair[0])
        self.queue.sync_permissions([])
        self.queue.sync_permissions(pair)
        self.assertFalse(self.queue.accepts(self.incoming))
        self.assertGreater(self.queue.access.authorization(*pair[0])["revision"], first["revision"])
        self.queue.close()
        self.open()
        self.queue.sync_permissions(pair)
        self.assertFalse(self.queue.accepts(self.incoming))

    def test_readonly_ingress_cannot_create_capture_rows(self):
        self.authorize()
        incoming = replace(self.incoming, capture_allowed=False)
        self.assertTrue(self.queue.accepts(incoming))
        with self.assertRaisesRegex(QueueError, "invalid_capture"):
            self.queue.capture(incoming, "conversation")
        for table in ("events", "outbox", "window", "replies"):
            self.assertEqual(self.queue.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_first_authorization_reauthorization_and_clock_rollback_keep_monotonic_floor(self):
        self.authorize()
        self.assertFalse(self.queue.accepts(replace(self.incoming, timestamp=100)))
        with patch.object(time, "time", return_value=200.25):
            self.queue.sync_permissions([])
            self.authorize()
        with patch.object(time, "time", return_value=150):
            self.queue.sync_permissions([])
            self.authorize()
        self.assertFalse(self.queue.accepts(replace(self.incoming, timestamp=200)))
        self.assertTrue(self.queue.accepts(replace(self.incoming, timestamp=201, received_at=201.5)))

    def test_identity_frozen_requests_and_operation_ids_survive_restart(self):
        self.authorize()
        before = self.queue.capture(self.incoming, "conversation")
        frozen = dict(self.queue.get(before.task_id))
        original_scope = dict(self.queue.scope(self.incoming))
        self.queue.close()
        self.open()
        with self.assertRaisesRegex(QueueError, "permissions_not_synced"):
            self.queue.capture_assistant(before.task_id, "new reply")
        self.queue.sync_permissions([("onebot", "2001")])
        self.assertTrue(self.queue.accepts(self.incoming))
        after_scope = self.queue.scope(self.incoming)
        for field in ("scope_id", "identity_json", "bank", "generation", "accept_from"):
            self.assertEqual(after_scope[field], original_scope[field])
        task = self.queue.get(before.task_id)
        for field in ("document_id", "operation_id", "payload", "payload_sha256"):
            self.assertEqual(task[field], frozen[field])

    def test_rebinding_blocks_old_queued_event_and_does_not_retarget_frozen_work(self):
        scope = self.authorize()
        capture = self.queue.capture(self.incoming, "conversation")
        original = dict(self.queue.get(capture.task_id))
        changed = replace(self.incoming, bot_id="1002", message_id="new")
        new_scope = self.authorize(changed)
        self.assertNotEqual(new_scope["bank"], scope["bank"])
        self.assertFalse(self.queue.accepts(self.incoming))
        self.assertFalse(self.queue.current(original))
        self.assertTrue(self.queue.current(original, writing=False))
        self.assertEqual(self.queue.get(capture.task_id)["operation_id"], original["operation_id"])
        self.assertEqual(self.queue.get(capture.task_id)["payload"], original["payload"])

    def test_observed_bot_change_revokes_binding_and_return_does_not_restore_it(self):
        self.authorize()
        self.assertTrue(self.queue.access.observe_bot("onebot", "aiocqhttp", "1002"))
        self.assertFalse(self.queue.accepts(self.incoming))
        self.queue.access.observe_bot("onebot", "aiocqhttp", "1001")
        self.queue.sync_permissions([("onebot", "2001")])
        self.assertFalse(self.queue.accepts(self.incoming))

    def test_retry_and_receipt_commit_together_and_survive_restart(self):
        scope = self.authorize()
        capture = self.queue.capture(self.incoming, "conversation")
        self.queue.update(capture.task_id, "failed", error="remote_failed")
        frozen = dict(self.queue.get(capture.task_id))
        command = self.queue.access.command(
            self.incoming, self.incoming.scope(self.queue.deployment_id), scope, "retry",
        )
        self.queue.access.run(
            command, scope, lambda: self.queue.request_retry(scope["scope_id"]),
        )
        self.assertTrue(self.queue.get(capture.task_id)["retry_requested"])
        self.queue.close()
        self.open()
        self.queue.sync_permissions([("onebot", "2001")])
        task = self.queue.get(capture.task_id)
        self.assertTrue(task["retry_requested"])
        self.assertEqual(task["operation_id"], frozen["operation_id"])
        self.assertEqual(task["payload"], frozen["payload"])
        self.assertEqual(self.queue.access.receipt(command)["result"], "applied")

    def test_entire_snapshot_restores_authorization_at_snapshot_time(self):
        self.authorize()
        self.queue.capture(self.incoming, "conversation")
        backup = self.root / "snapshot.sqlite3"
        import sqlite3
        with closing(sqlite3.connect(backup)) as target:
            self.queue.db.backup(target)
        self.queue.sync_permissions([])
        self.queue.close()
        shutil.copyfile(backup, self.root / "runtime/bridge.sqlite3")
        self.open()
        self.queue.sync_permissions([("onebot", "2001")])
        self.assertTrue(self.queue.scope(self.incoming)["allowed"])
        self.assertTrue(self.queue.accepts(self.incoming))

    def test_retry_recovers_the_same_failed_operation_after_restart(self):
        scope = self.authorize()
        capture = self.queue.capture(self.incoming, "conversation")
        frozen = dict(self.queue.get(capture.task_id))
        self.queue.update(capture.task_id, "tracking", accepted=1)
        self.queue.update(capture.task_id, "failed", error="remote_failed")
        self.queue.request_retry(scope["scope_id"])
        self.queue.close()
        self.open()
        self.queue.sync_permissions([("onebot", "2001")])
        restored = self.queue.get(capture.task_id)
        self.assertEqual(restored["state"], "failed")
        self.assertTrue(restored["retry_requested"])

        async def retry(bank, operation_id, *, before_send):
            await before_send()

        client = SimpleNamespace(
            operation=AsyncMock(return_value=Operation(frozen["operation_id"], "failed")),
            retry=AsyncMock(side_effect=retry),
        )
        worker = Worker(self.queue, client, Mock())
        async def recover():
            self.assertTrue(await worker.maintenance.startup())
            await worker.process(self.queue.get(capture.task_id))
        asyncio.run(recover())
        client.retry.assert_awaited_once()
        self.assertEqual(client.retry.await_args.args, (frozen["bank"], frozen["operation_id"]))
        self.assertEqual(self.queue.get(capture.task_id)["payload"], frozen["payload"])
        self.assertFalse(self.queue.get(capture.task_id)["retry_requested"])
        self.assertEqual(self.queue.control.db.execute(
            "SELECT pending FROM retry_intents WHERE operation_id=?", (frozen["operation_id"],),
        ).fetchone()[0], 0)

    def test_mode_receipt_and_control_change_are_atomic_and_replay_is_persistent(self):
        scope = self.authorize()
        source = self.incoming.scope(self.queue.deployment_id)
        command_event = replace(self.incoming, message_id="pause", text="/hb pause")
        command = self.queue.access.command(command_event, source, scope, "mode", ("paused",))
        self.queue.access.run(
            command, scope, lambda: self.queue.control.set_mode("service", scope["owner_id"], "paused"),
        )
        with self.assertRaisesRegex(QueueError, "command_already_applied"):
            self.queue.access.command(command_event, source, self.queue.scope(self.incoming), "mode", ("paused",))
        self.queue.close()
        self.open()
        self.queue.sync_permissions([("onebot", "2001")])
        with self.assertRaisesRegex(QueueError, "command_already_applied"):
            self.queue.access.command(command_event, source, self.queue.scope(self.incoming), "mode", ("paused",))
        with self.assertRaisesRegex(QueueError, "command_identity_conflict"):
            self.queue.access.command(
                replace(command_event, text="/hb on"), source,
                self.queue.scope(self.incoming), "mode", ("active",),
            )
        receipt = dict(self.queue.access.receipt(command))
        self.assertNotIn("/hb", json.dumps(receipt))

    def test_failed_atomic_change_does_not_consume_command(self):
        scope = self.authorize()
        command = self.queue.access.command(
            self.incoming, self.incoming.scope(self.queue.deployment_id), scope, "mode", ("paused",),
        )
        def failure():
            self.queue.control.set_mode("service", scope["owner_id"], "paused")
            raise OSError("disk")
        with self.assertRaises(OSError):
            self.queue.access.run(command, scope, failure)
        self.assertIsNone(self.queue.access.receipt(command))
        self.assertEqual(self.queue.control.owner("service", scope["owner_id"])["mode"], "active")

    def test_new_message_keys_can_apply_in_the_same_second_or_arrive_out_of_order(self):
        scope = self.authorize()
        with patch.object(time, "time", return_value=103.25):
            self.queue.set_mode(scope["scope_id"], "paused")
        scope = self.queue.scope(self.incoming)
        source = self.incoming.scope(self.queue.deployment_id)
        for index, timestamp in enumerate((103, 103, 101)):
            scope = self.queue.scope(self.incoming)
            command = self.queue.access.command(
                replace(self.incoming, message_id=f"new-{index}", timestamp=timestamp), source, scope, "mode", ("active",),
            )
            self.queue.access.run(command, scope, lambda: self.queue.set_mode(
                scope["scope_id"], "active",
            ))
            self.assertEqual(self.queue.scope(self.incoming)["mode"], "active")

    def test_resume_and_forget_keep_message_boundaries_without_command_clock_high_water(self):
        self.authorize()
        source = self.incoming.scope(self.queue.deployment_id)
        for mode, stamp in (("paused", 103), ("active", 105)):
            scope = self.queue.scope(self.incoming)
            command = self.queue.access.command(
                replace(self.incoming, message_id=mode, timestamp=stamp, received_at=stamp + 0.5),
                source, scope, "mode", (mode,),
            )
            with patch.object(time, "time", return_value=stamp + 0.5):
                self.queue.access.run(command, scope, lambda: self.queue.set_mode(
                    scope["scope_id"], mode,
                ))
        self.assertEqual(self.queue.scope(self.incoming)["accept_from"], 106)
        for stamp in (103, 104, 105):
            with self.assertRaisesRegex(QueueError, "message_before_accept_from"):
                self.queue.capture(replace(self.incoming, timestamp=stamp), "conversation")
        self.queue.forget(self.queue.scope(self.incoming)["scope_id"])
        self.assertGreaterEqual(self.queue.scope(self.incoming)["accept_from"], 106)

    def test_host_queue_time_blocks_platform_clock_ahead_before_authorization(self):
        before_grant = replace(self.incoming, timestamp=105, received_at=99.5)
        self.authorize()
        self.assertFalse(self.queue.accepts(before_grant))

    def test_legacy_disabled_scope_needs_explicit_new_authorization(self):
        old = self.queue.scope(self.incoming)
        with self.queue.db:
            self.queue.db.execute("UPDATE authorizations SET enabled=0")
        with patch.object(time, "time", return_value=200.25):
            self.queue.sync_permissions([("onebot", "2001")])
            self.assertFalse(self.queue.accepts(replace(self.incoming, timestamp=150)))
            self.assertFalse(self.queue.access.authorization("onebot", "2001")["enabled"])
            activated = self.authorize()
        self.assertEqual(activated["bank"], old["bank"])
        self.assertFalse(self.queue.accepts(replace(self.incoming, timestamp=200)))
        self.assertTrue(self.queue.accepts(replace(self.incoming, timestamp=201, received_at=201.5)))

    def test_command_before_forget_cannot_apply_to_new_generation(self):
        scope = self.authorize()
        source = self.incoming.scope(self.queue.deployment_id)
        command = self.queue.access.command(self.incoming, source, scope, "mode", ("active",))
        self.queue.forget(scope["scope_id"], now=102.25)
        with self.assertRaisesRegex(QueueError, "command_expired"):
            self.queue.access.run(command, scope, lambda: None)
        fresh = self.queue.access.command(self.incoming, source, self.queue.scope(self.incoming), "mode", ("active",))
        self.assertNotEqual(fresh.generation, command.generation)

    def test_historical_management_never_grants_chat_or_submit_access(self):
        scope = self.authorize()
        capture = self.queue.capture(self.incoming, "conversation")
        self.queue.sync_permissions([])
        management = self.queue.management(scope["scope_id"])
        self.assertFalse(self.queue.can_recall(self.queue.scope_owner(scope["scope_id"]), manual=True))
        self.assertFalse(self.queue.current(self.queue.get(capture.task_id)))
        with self.assertRaisesRegex(QueueError, "scope_not_available"):
            self.queue.forget(scope["scope_id"], management=True)
        with self.assertRaisesRegex(QueueError, "scope_not_active"):
            self.queue.request_retry(scope["scope_id"])
        maintenance = Maintenance(self.queue, None)
        maintenance.begin(scope["scope_id"], discard_pending=True, management=management)
        self.assertEqual(self.queue.get(capture.task_id)["state"], "discarded")
        fresh = self.queue.management(scope["scope_id"])
        with self.assertRaisesRegex(QueueError, "scope_not_available"):
            self.queue.forget(scope["scope_id"], management=management)
        erasure = self.queue.forget(scope["scope_id"], management=fresh)
        self.assertEqual(erasure["old_bank"], scope["bank"])
        self.assertFalse(self.queue.scope_owner(scope["scope_id"])["allowed"])


class SourceContracts(unittest.TestCase):
    class Plain:
        def __init__(self, text):
            self.text = text

    def event(self, text="/hb pause"):
        raw = {
            "post_type": "message", "message_type": "private", "self_id": 10,
            "user_id": 20, "message_id": 30, "time": 100,
            "message": [{"type": "text", "data": {"text": text}}],
        }
        return SimpleNamespace(
            bot=object(), get_platform_name=lambda: "aiocqhttp",
            created_at=time.time(),
            get_platform_id=lambda: "onebot", get_self_id=lambda: "10",
            get_sender_id=lambda: "20", is_private_chat=lambda: True, get_group_id=lambda: "",
            get_message_str=lambda: text.strip(), get_messages=lambda: [self.Plain(text.strip())],
            message_obj=SimpleNamespace(raw_message=raw, message_id="30"),
        )

    def test_commands_and_capture_share_original_identity_time_and_content(self):
        event = self.event()
        header = native_header(event, self.Plain)
        self.assertEqual(header.timestamp, 100)
        self.assertEqual(header.message_id, "30")
        event.message_obj.raw_message["time"] = None
        self.assertIsNone(native_header(event, self.Plain))

    def test_host_queue_evidence_is_required_and_preserved(self):
        event = self.event("hello")
        event.created_at = time.time() - 10
        header = native_header(event, self.Plain)
        self.assertEqual(header.received_at, event.created_at)
        for value in (None, True, float("nan"), float("inf"), 0, time.time() + 301):
            event.created_at = value
            self.assertIsNone(native_header(event, self.Plain))

    def test_host_queue_future_tolerance_is_independent_of_platform_clock_skew(self):
        with patch.object(time, "time", return_value=100.25):
            event = self.event("hello")
            event.message_obj.raw_message["time"] = 105
            event.created_at = 100.75
            self.assertEqual(native_header(event, self.Plain).timestamp, 105)
            event.created_at = 101.5
            self.assertEqual(native_header(event, self.Plain).timestamp, 105)
            event.created_at = 400.5
            self.assertIsNone(native_header(event, self.Plain))

    def test_host_removed_wake_prefix_requires_exact_public_configuration(self):
        event = self.event()
        event.get_message_str = lambda: "hb pause"
        event.is_at_or_wake_command = True
        self.assertIsNone(native_header(event, self.Plain))
        self.assertEqual(native_header(event, self.Plain, ["/"]).text, "/hb pause")
        self.assertIsNone(native_header(event, self.Plain, ["!"]))

    def test_new_materials_keep_platform_time_provenance_and_assistant_observation(self):
        base = Incoming("aiocqhttp", "onebot", "10", "20", "30", 101, "hello")
        for kind, time_source in (
            ("aiocqhttp", "onebot.time"), ("qq_official", "qq_official.timestamp"),
            ("weixin_oc", "weixin_oc.create_time_ms"), ("telegram", "telegram.message.date"),
        ):
            with self.subTest(kind=kind):
                source = replace(base, platform_type=kind).scope("deployment")
                for assistant in (False, True):
                    material = retain_payload(
                        source, 0, Message("event", "hello", 101, "assistant" if assistant else "user"),
                        (), "conversation", "operation", "UTC", assistant=assistant,
                    )["items"][0]
                    self.assertEqual(material["timestamp"], "1970-01-01T00:01:41+00:00")
                    self.assertEqual(material["metadata"]["time_source"], "history_observed_at" if assistant else time_source)

    def test_old_event_producer_is_not_assigned_current_client(self):
        event = self.event("hello")
        adapter = SimpleNamespace(
            get_client=lambda: event.bot,
            meta=lambda: SimpleNamespace(id=event.get_platform_id(), name="aiocqhttp"),
        )
        with patch.object(
            onebot_source, "native_types", return_value=(SimpleNamespace, SimpleNamespace, type(event.bot), dict),
        ):
            self.assertEqual(native_header(event, self.Plain, adapter=adapter).bot_id, "10")
            adapter.get_client = lambda: object()
            self.assertIsNone(native_header(event, self.Plain, adapter=adapter))
            adapter.get_client = lambda: event.bot
            self.assertEqual(native_header(event, self.Plain, adapter=adapter).bot_id, "10")

    def test_unreleased_platforms_and_opaque_ids_do_not_inherit_onebot_rules(self):
        self.assertTrue(platform_details("aiocqhttp")["supported"])
        self.assertTrue(platform_details("qq_official")["supported"])
        self.assertTrue(platform_details("qq_official_webhook")["supported"])
        self.assertTrue(platform_details("weixin_oc")["supported"])
        self.assertTrue(platform_details("telegram")["supported"])
        self.assertFalse(platform_details("unknown")["supported"])
        self.assertTrue(valid_user_id("qq_official", "AbC_x-123"))
        self.assertFalse(valid_user_id("aiocqhttp", "AbC_x-123"))
        self.assertFalse(valid_user_id("aiocqhttp", "１２３"))
        self.assertFalse(valid_user_id("qq_official", "openid:FriendMessage:other"))
        self.assertFalse(valid_user_id("qq_official", "openid\n"))
