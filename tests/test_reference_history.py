import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from plugins.astrbot_plugin_hindsight_bridge.core.control import QueueError
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming
from plugins.astrbot_plugin_hindsight_bridge.core.materials import DEFAULT_MEMORY_PROMPT
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox
from plugins.astrbot_plugin_hindsight_bridge.core.reference import reference_options, save_reference_options
from queue_fixture import confirm_sources


class ReferenceIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "runtime"
        self.queue = self.open_queue()
        self.addCleanup(lambda: self.queue.close())
        confirm_sources(self.queue, ("qq", "bot", "alice"), ("qq", "bot", "bob"))
        self.alice = self.scope("alice")
        self.bob = self.scope("bob")
        self.payload = {"kind": "request", "text": "delivered", "status": "delivered"}

    def open_queue(self):
        return Outbox(self.directory, "service", "http://memory.test", credential_fingerprint="key")

    def scope(self, user):
        return self.queue.scope(Incoming("aiocqhttp", "qq", "bot", user, "message", 1, "hello"))

    def test_revoked_binding_cannot_read_or_append_to_either_users_turns(self):
        alice_id = self.queue.references.start(self.alice, "chat", "one", self.payload)
        bob_id = self.queue.references.start(self.bob, "chat", "one", self.payload)
        self.queue.references.append(self.alice, bob_id, {"text": "wrong owner"})
        with self.assertRaisesRegex(QueueError, "reference_turn_missing"):
            self.queue.references.detail(self.alice, bob_id)
        self.queue.sync_permissions([("qq", "bob")])
        confirm_sources(self.queue, ("qq", "bot", "alice"), ("qq", "bot", "bob"))
        self.queue.references.append(self.alice, alice_id, {"text": "expired turn"})
        self.alice = self.scope("alice")
        self.assertEqual(self.queue.references.list(self.alice), [])
        with self.assertRaisesRegex(QueueError, "reference_turn_missing"):
            self.queue.references.detail(self.alice, alice_id)
        self.assertEqual(self.queue.references.detail(self.bob, bob_id)["deliveries"], [self.payload])

    def test_upgrade_cleans_old_options_and_snapshot_after_legacy_forget(self):
        options = reference_options(self.queue, self.alice)
        save_reference_options(self.queue, self.alice, **{**options, "prompt": "old private prompt"},
                               tokenizer="o200k_base")
        self.queue.references.start(self.alice, "chat", "old", self.payload)
        bob_id = self.queue.references.start(self.bob, "chat", "keep", self.payload)
        key, old_value = self.queue.control.db.execute(
            "SELECT key,value FROM settings WHERE key GLOB 'reference:*'"
        ).fetchone()
        snapshot = Path(self.tmp.name) / "old.sqlite3"
        self.queue.references.close()
        shutil.copyfile(self.queue.references.path, snapshot)
        self.queue.forget(self.alice["scope_id"])
        self.queue.close()
        with sqlite3.connect(self.directory.with_name("runtime-control") / "control.sqlite3") as control:
            control.execute("UPDATE settings SET value=? WHERE key=?", (old_value, key))
        shutil.copyfile(snapshot, self.directory / "references.sqlite3")
        self.queue = self.open_queue()
        self.queue.sync_permissions([("qq", "alice"), ("qq", "bob")])
        self.assertNotIn("old private prompt", self.queue.control.setting(key))
        self.assertEqual(reference_options(self.queue, self.scope("alice"))["prompt"], DEFAULT_MEMORY_PROMPT)
        with sqlite3.connect(self.queue.references.path) as database:
            self.assertEqual(database.execute("SELECT id FROM turns").fetchall(), [(bob_id,)])
            self.assertEqual(database.execute("SELECT turn_id FROM deliveries").fetchall(), [(bob_id,)])

    def test_corrupt_options_affect_only_their_owner_and_can_be_forgotten(self):
        options = reference_options(self.queue, self.alice)
        save_reference_options(self.queue, self.alice, **options, tokenizer="o200k_base")
        self.queue.apply_control()
        old_id = self.queue.references.start(self.alice, "chat", "private", self.payload)
        bob_id = self.queue.references.start(self.bob, "chat", "keep", self.payload)
        self.queue.close()
        with sqlite3.connect(self.directory.with_name("runtime-control") / "control.sqlite3") as control:
            control.execute("UPDATE settings SET value='broken' WHERE key GLOB 'reference:*'")
        self.queue = self.open_queue()
        self.queue.sync_permissions([("qq", "alice"), ("qq", "bob")])
        self.assertEqual(self.queue.references.detail(self.bob, bob_id)["deliveries"], [self.payload])
        with self.assertRaisesRegex(QueueError, "reference_settings_invalid"):
            self.queue.references.list(self.alice)
        self.queue.forget(self.alice["scope_id"], journal_only=True)
        self.queue.references.append(self.alice, old_id, {"text": "late"})
        with self.assertRaisesRegex(QueueError, "scope_not_available"):
            self.queue.references.list(self.alice)
        self.queue.apply_control()
        self.assertEqual(self.queue.references.list(self.scope("alice")), [])
        with sqlite3.connect(self.queue.references.path) as database:
            self.assertEqual(database.execute("SELECT turn_id FROM deliveries").fetchall(), [(bob_id,)])
