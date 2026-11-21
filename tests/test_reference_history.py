import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
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

    def test_legacy_options_default_and_legacy_saves_preserve_new_user_settings(self):
        options = reference_options(self.queue, self.alice)
        self.assertEqual((options["recall_budget"], options["capture_assistant"]), ("mid", True))
        saved = save_reference_options(self.queue, self.alice, **{
            **options, "recall_budget": "high", "capture_assistant": False, "recall_on_quoted_or_link": True,
        })
        legacy = {key: value for key, value in saved.items()
                  if key not in {"recall_budget", "capture_assistant", "recall_on_quoted_or_link"}}
        saved = save_reference_options(self.queue, self.alice, **{**legacy, "exclude_assistant": True})
        self.assertEqual((saved["recall_budget"], saved["capture_assistant"], saved["exclude_assistant"]),
                         ("high", False, True))
        self.assertEqual(reference_options(self.queue, self.alice), saved)
        self.assertTrue(saved["recall_on_quoted_or_link"])
        self.assertFalse(options["recall_on_quoted_or_link"])
        self.assertEqual(reference_options(self.queue, self.bob), options)
        for invalid in ({"recall_budget": "medium"}, {"recall_budget": []}, {"capture_assistant": 0},
                        {"recall_on_quoted_or_link": 1}):
            with self.assertRaisesRegex(QueueError, "reference_settings_invalid"):
                save_reference_options(self.queue, self.alice, **{**saved, **invalid})
        key, value = self.queue.control.db.execute(
            "SELECT key,value FROM settings WHERE key GLOB 'reference:*'"
        ).fetchone()
        legacy = {name: value for name, value in json.loads(value).items()
                  if name not in {"recall_budget", "capture_assistant", "recall_on_quoted_or_link"}}
        with self.queue.control.db:
            self.queue.control.db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(legacy), key))
        restored = reference_options(self.queue, self.alice)
        self.assertEqual((restored["recall_budget"], restored["capture_assistant"], restored["exclude_assistant"]),
                         ("mid", True, True))
        self.assertFalse(restored["recall_on_quoted_or_link"])

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
        save_reference_options(self.queue, self.alice, **{**options, "prompt": "old private prompt"})
        self.queue.references.start(self.alice, "chat", "old", self.payload)
        bob_id = self.queue.references.start(self.bob, "chat", "keep", self.payload)
        key, old_value = self.queue.control.db.execute(
            "SELECT key,value FROM settings WHERE key GLOB 'reference:*'"
        ).fetchone()
        bob_options = reference_options(self.queue, self.bob)
        save_reference_options(self.queue, self.bob, **{**bob_options, "prompt": "Bob's prompt"})
        snapshot = Path(self.tmp.name) / "old.sqlite3"
        self.queue.references.close()
        shutil.copyfile(self.queue.references.path, snapshot)
        self.queue.forget(self.alice["scope_id"])
        stored = json.loads(old_value)
        cases = {
            "valid": old_value,
            "invalid history limit": json.dumps({**stored, "history_limit": "invalid"}),
            "invalid binding": json.dumps({**stored, "binding": []}),
            "missing binding": json.dumps({key: value for key, value in stored.items() if key != "binding"}),
            "invalid revision": json.dumps({**stored, "revision": None}),
            "truncated JSON": '{"prompt":"old private prompt",',
        }
        for case, value in cases.items():
            with self.subTest(case=case):
                self.queue.close()
                with closing(sqlite3.connect(self.directory.with_name("runtime-control") / "control.sqlite3")) as control, control:
                    control.execute("UPDATE settings SET value=? WHERE key=?", (value, key))
                shutil.copyfile(snapshot, self.directory / "references.sqlite3")
                self.queue = self.open_queue()
                self.queue.sync_permissions([("qq", "alice"), ("qq", "bob")])
                tombstone = json.loads(self.queue.control.setting(key))
                self.assertEqual(set(tombstone), {"revision"})
                self.assertGreater(tombstone["revision"], stored["revision"])
                self.assertEqual(reference_options(self.queue, self.scope("alice"))["prompt"], DEFAULT_MEMORY_PROMPT)
                self.assertEqual(reference_options(self.queue, self.bob)["prompt"], "Bob's prompt")
                with closing(sqlite3.connect(self.queue.references.path)) as database:
                    self.assertEqual(database.execute("SELECT id FROM turns").fetchall(), [(bob_id,)])
                    self.assertEqual(database.execute("SELECT turn_id FROM deliveries").fetchall(), [(bob_id,)])

    def test_corrupt_options_affect_only_their_owner_and_can_be_forgotten(self):
        options = reference_options(self.queue, self.alice)
        old_id = self.queue.references.start(self.alice, "chat", "private", self.payload)
        latest_id = self.queue.references.start(self.alice, "chat", "latest", self.payload)
        bob_id = self.queue.references.start(self.bob, "chat", "keep", self.payload)
        snapshot = Path(self.tmp.name) / "old.sqlite3"
        self.queue.references.close()
        shutil.copyfile(self.queue.references.path, snapshot)
        save_reference_options(self.queue, self.alice, **{**options, "history_limit": 0})
        self.queue.apply_control()
        key, value = self.queue.control.db.execute(
            "SELECT key,value FROM settings WHERE key GLOB 'reference:*'"
        ).fetchone()
        cases = (
            ({"revision": None}, {bob_id}),
            ({"types": ["broken"]}, {bob_id}),
            ({"types": ["broken"], "history_limit": 1}, {bob_id, latest_id}),
            ({"history_limit": "broken"}, {bob_id, old_id, latest_id}),
        )
        for damaged, retained in cases:
            with self.subTest(damaged=damaged):
                self.queue.close()
                with closing(sqlite3.connect(self.directory.with_name("runtime-control") / "control.sqlite3")) as control, control:
                    control.execute("UPDATE settings SET value=? WHERE key=?", (
                        json.dumps({**json.loads(value), **damaged}), key,
                    ))
                shutil.copyfile(snapshot, self.directory / "references.sqlite3")
                self.queue = self.open_queue()
                self.queue.sync_permissions([("qq", "alice"), ("qq", "bob")])
                self.assertEqual(self.queue.references.detail(self.bob, bob_id)["deliveries"], [self.payload])
                with self.assertRaisesRegex(QueueError, "reference_settings_invalid"):
                    self.queue.references.list(self.alice)
                self.assertIsNone(self.queue.references.start(self.alice, "chat", "new", self.payload))
                with closing(sqlite3.connect(self.queue.references.path)) as database:
                    self.assertEqual({row[0] for row in database.execute("SELECT id FROM turns")}, retained)
                    self.assertEqual({row[0] for row in database.execute("SELECT turn_id FROM deliveries")}, retained)
        self.queue.forget(self.alice["scope_id"], journal_only=True)
        self.queue.references.append(self.alice, old_id, {"text": "late"})
        with self.assertRaisesRegex(QueueError, "scope_not_available"):
            self.queue.references.list(self.alice)
        self.queue.apply_control()
        self.assertEqual(self.queue.references.list(self.scope("alice")), [])
        with closing(sqlite3.connect(self.queue.references.path)) as database:
            self.assertEqual(database.execute("SELECT turn_id FROM deliveries").fetchall(), [(bob_id,)])
