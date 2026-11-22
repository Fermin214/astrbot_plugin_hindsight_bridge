from contextlib import ExitStack, closing
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from legacy_fixture import legacy_code
from plugins.astrbot_plugin_hindsight_bridge.core import storage
from plugins.astrbot_plugin_hindsight_bridge.core.background import reference, reference_key
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming, canonical
from plugins.astrbot_plugin_hindsight_bridge.core.maintenance import Maintenance
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox, QueueError
from plugins.astrbot_plugin_hindsight_bridge.core.reference import _scope_binding


class MainDatabase(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.root = Path(self.resources.enter_context(tempfile.TemporaryDirectory()))
        self.queue = Outbox(self.root / "runtime", "service", "http://memory.test", credential_fingerprint="test")
        self.addCleanup(lambda: self.queue.close())
        self.queue.sync_permissions([("onebot", "alice"), ("onebot", "bob")])
        self.incoming = Incoming("aiocqhttp", "onebot", "bot", "alice", "one", int(time.time()) + 1, "正文")
        self.queue.activate(self.incoming, self.queue.access.authorization("onebot", "alice")["revision"])
        self.incoming = replace(self.incoming, received_at=time.time() + 1)

    def test_windows_file_sync_uses_writeable_handle_and_skips_directories(self):
        path = self.root / "snapshot"
        path.write_bytes(b"committed snapshot")
        with patch.object(storage.os, "name", "nt"), patch.object(storage.os, "open", wraps=os.open) as opened:
            storage.sync_path(path)
            opened.assert_called_once_with(path, os.O_RDWR)
            opened.reset_mock()
            storage.sync_path(self.root)
            opened.assert_not_called()
        self.assertEqual(path.read_bytes(), b"committed snapshot")

    def test_shared_connection_and_durable_pragmas(self):
        self.assertIs(self.queue.db, self.queue.control.db)
        self.assertEqual(self.queue.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.queue.db.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertFalse((self.root / "runtime-control").exists())
        self.queue.capture(self.incoming, "conversation")
        if os.name != "nt":
            self.assertEqual((self.root / "runtime/bridge.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_nested_failure_rolls_back_receipt_queue_and_owner(self):
        scope = self.queue.scope(self.incoming)
        capture = self.queue.capture(self.incoming, "conversation")
        command = self.queue.access.command(self.incoming, self.incoming.scope(self.queue.deployment_id), scope, "forget")

        def failing():
            self.queue.forget(scope["scope_id"])
            raise sqlite3.OperationalError("injected")

        with self.assertRaises(sqlite3.OperationalError):
            self.queue.access.run(command, scope, failing)
        self.assertIsNone(self.queue.access.receipt(command))
        self.assertEqual(self.queue.scope_owner(scope["scope_id"])["bank"], capture.bank)
        self.assertEqual(self.queue.get(capture.task_id)["state"], "ready")
        self.assertTrue(self.queue.get(capture.task_id)["payload"])
        self.assertFalse(self.queue.control.erasures("service"))
        self.assertTrue(self.queue.db.execute("SELECT 1 FROM window").fetchone())

    def test_dispatch_cancel_and_ack_are_atomic(self):
        capture = self.queue.capture(self.incoming, "conversation")
        task = dict(self.queue.get(capture.task_id))
        with patch.object(self.queue, "update", side_effect=sqlite3.OperationalError("injected")):
            with self.assertRaises(sqlite3.OperationalError):
                self.queue.before_submit(task)
        self.assertIsNone(self.queue.control.request(task["operation_id"]))
        self.assertEqual(self.queue.get(capture.task_id)["state"], "ready")
        self.queue.before_submit(task)
        self.assertEqual(self.queue.get(capture.task_id)["state"], "submitting")
        self.assertTrue(self.queue.control.request(task["operation_id"])["dispatched"])
        self.queue.update(capture.task_id, "tracking", accepted=1)
        self.assertTrue(self.queue.control.request(task["operation_id"])["accepted"])
        self.assertEqual(self.queue.request_cancel(capture.scope_id, capture.task_id), "cancel_requested")
        self.assertTrue(self.queue.control.request(task["operation_id"])["cancel_intent"])
        self.assertTrue(self.queue.get(capture.task_id)["cancel_intent"])

    def test_clear_is_immediate_and_revocation_invalidates_pages(self):
        capture = self.queue.capture(self.incoming, "conversation")
        scope = self.queue.scope_owner(capture.scope_id)
        erasure = self.queue.forget(capture.scope_id)
        self.assertEqual(self.queue.scope_owner(capture.scope_id)["bank"], erasure["new_bank"])
        self.assertEqual(self.queue.get(capture.task_id)["state"], "discarded")
        self.assertEqual(self.queue.get(capture.task_id)["payload"], "")
        self.assertFalse(self.queue.db.execute("SELECT 1 FROM window").fetchone())
        self.assertIn(capture.bank, {row["old_bank"] for row in self.queue.control.erasures("service")})
        self.queue.sync_permissions([])
        self.assertGreater(self.queue.scope_owner(capture.scope_id)["revision"], scope["revision"])
        self.assertFalse(self.queue.can_recall(scope))

    def test_maintenance_failure_rolls_back_pause_and_discard(self):
        capture = self.queue.capture(self.incoming, "conversation")
        scope = self.queue.scope_owner(capture.scope_id)
        maintenance = Maintenance(self.queue, None)
        with patch.object(self.queue, "update", side_effect=sqlite3.OperationalError("injected")):
            with self.assertRaises(sqlite3.OperationalError):
                maintenance.begin(capture.scope_id, discard_pending=True)
        self.assertIsNone(self.queue.control.maintenance("service", capture.bank))
        self.assertEqual(self.queue.get(capture.task_id)["state"], "ready")
        self.assertEqual(self.queue.scope_owner(capture.scope_id)["revision"], scope["revision"])
        self.assertIsNone(self.queue.control.request(self.queue.get(capture.task_id)["operation_id"]))

    def test_unconfirmed_login_suspends_writes_without_revoking_or_erasing_history(self):
        capture = self.queue.capture(self.incoming, "conversation")
        self.queue.release(capture.task_id)
        scope = self.queue.scope_owner(capture.scope_id)
        grant = dict(self.queue.access.authorization("onebot", "alice"))
        turn = self.queue.references.start(scope, "conversation", "one", {"kind": "request", "status": "no_match"})
        self.queue.suspended_sources.add(capture.scope_id)
        self.assertFalse(self.queue.current(self.queue.get(capture.task_id)))
        self.assertFalse(self.queue.can_recall(scope))
        self.assertIsNone(self.queue.due())
        self.queue.references.reconcile(force=True)
        self.assertEqual(dict(self.queue.access.authorization("onebot", "alice")), grant)
        self.queue.suspended_sources.clear()
        self.assertTrue(self.queue.current(self.queue.get(capture.task_id)))
        self.assertEqual(self.queue.references.list(scope)[0]["id"], turn)


class Migration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resources = ExitStack()
        cls.legacy = cls.resources.enter_context(legacy_code())
        cls.addClassCleanup(cls.resources.close)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime, self.control = self.root / "runtime", self.root / "runtime-control"
        with patch.object(time, "time", return_value=1000.25):
            old = self.legacy.Outbox(self.runtime, "service", "http://memory.test", credential_fingerprint="test")
            try:
                old.sync_permissions([("onebot", name) for name in ("alice", "bob", "carol")])
                self.scopes, self.tasks = {}, {}
                for name in ("alice", "bob", "carol"):
                    incoming = self.legacy.Incoming("aiocqhttp", "onebot", "bot", name, name, 1010, "正文", received_at=1010.5)
                    scope = old.activate(incoming, old.access.authorization("onebot", name)["revision"], now=0)
                    capture = old.capture(incoming, "conversation")
                    self.scopes[name], self.tasks[name] = dict(scope), dict(old.get(capture.task_id))
                alice, bob = self.tasks["alice"], self.tasks["bob"]
                old.update(alice["id"], "submitting")
                old.control.record_request(alice, self.scopes["alice"]["owner_id"], dispatched=True, accepted=True)
                old.update(bob["id"], "failed", error="remote_failed")
                old.request_retry(self.scopes["bob"]["scope_id"], journal_only=True)
                scope = self.scopes["alice"]
                for index in range(3):
                    old.references.start(scope, "conversation", str(index), {"kind": "request", "status": "no_match", "items": []})
                self.binding = _scope_binding(old, scope)
                self.turns = old.references.list(scope)
                grant = old.access.authorization("onebot", "alice")
                with old.control.transaction():
                    old.control.db.execute("INSERT INTO settings VALUES (?,?)", (
                        reference_key("service", scope["owner_id"]), canonical({
                            "format": 1, "revision": 7, "enabled": True,
                            "page_id": "page-one", "mental_model_id": "model-one",
                            "source_id": scope["scope_id"], "authorization_revision": grant["revision"],
                            "bank": scope["bank"], "generation": scope["generation"],
                        }),
                    ))
                self.erasure = dict(old.forget(self.scopes["carol"]["scope_id"], journal_only=True))
            finally:
                old.close()

    def open(self):
        return Outbox(self.runtime, "service", "http://memory.test", credential_fingerprint="test")

    def assert_originals(self):
        for path in (self.runtime / "runtime.sqlite3", self.control / "control.sqlite3"):
            self.assertTrue(path.is_file())
            with closing(sqlite3.connect(path)) as database:
                self.assertEqual(database.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def assert_migrated(self):
        with closing(self.open()) as queue:
            queue.sync_permissions([("onebot", name) for name in ("alice", "bob", "carol")])
            for name, old in self.tasks.items():
                task = queue.get(old["id"])
                for key in ("id", "operation_id", "document_id", "bank", "generation", "created_at"):
                    self.assertEqual(task[key], old[key])
            self.assertEqual(queue.get(self.tasks["alice"]["id"])["state"], "unknown")
            self.assertTrue(queue.get(self.tasks["alice"]["id"])["accepted"])
            self.assertTrue(queue.get(self.tasks["bob"]["id"])["retry_requested"])
            self.assertEqual(queue.get(self.tasks["carol"]["id"])["payload"], "")
            self.assertEqual(queue.scope_owner(self.scopes["carol"]["scope_id"])["bank"], self.erasure["new_bank"])
            scope = queue.scope_owner(self.scopes["alice"]["scope_id"])
            self.assertEqual(_scope_binding(queue, scope), self.binding)
            self.assertEqual([turn["id"] for turn in queue.references.list(scope)], [turn["id"] for turn in self.turns])
            self.assertEqual(reference(queue, scope)["revision"], 7)
            self.assertEqual(reference(queue, scope)["pages"], [{"page_id": "page-one", "mental_model_id": "model-one"}])
            self.assertEqual(json.loads(queue.control.setting(reference_key("service", scope["owner_id"])))["format"], 2)

    def test_real_v050_data_preserves_ids_history_and_settings(self):
        self.assert_migrated()
        self.assertFalse((self.runtime / "runtime.sqlite3").exists())
        self.assertFalse((self.control / "control.sqlite3").exists())
        backup = self.runtime / storage.BACKUP
        manifest = json.loads((backup / "manifest.json").read_text())
        self.assertIn("runtime/runtime.sqlite3", manifest["files"])
        self.assertIn("control/control.sqlite3", manifest["files"])
        self.assertTrue(all("\\" not in name for name in manifest["files"]))
        self.assert_migrated()

    def test_missing_mismatch_rollback_and_space_abort_without_new_identity(self):
        for error in ("missing", "identity", "rollback", "space"):
            with self.subTest(error=error), ExitStack() as resources:
                path = self.control / "control.sqlite3"
                original = path.read_bytes()
                resources.callback(path.write_bytes, original)
                if error == "missing":
                    path.unlink()
                elif error in {"identity", "rollback"}:
                    with closing(sqlite3.connect(path)) as database, database:
                        database.execute("UPDATE settings SET value=? WHERE key=?", ("wrong" if error == "identity" else "0", "control_id" if error == "identity" else "revision"))
                else:
                    resources.enter_context(patch.object(shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 100, 0)))
                with self.assertRaises(QueueError):
                    self.open()
                self.assertFalse((self.runtime / "bridge.sqlite3").exists())
        self.assert_originals()
        self.assert_migrated()

    def test_backup_and_import_interruption_resume(self):
        for stage in ("backup", "import"):
            with self.subTest(stage=stage):
                target, attribute = (shutil, "copytree") if stage == "backup" else (storage, "copy_tables")
                with patch.object(target, attribute, side_effect=OSError("injected")):
                    with self.assertRaises(OSError):
                        self.open()
                self.assert_originals()
                self.assertFalse((self.runtime / "bridge.sqlite3").exists())
        self.assert_migrated()

    def test_cutover_interruption_never_reimports(self):
        original = storage.sync_path

        def interrupt(path):
            if (self.runtime / "bridge.sqlite3").exists():
                raise OSError("injected")
            return original(path)

        with patch.object(storage, "sync_path", side_effect=interrupt):
            with self.assertRaises(OSError):
                self.open()
        self.assert_originals()
        with patch.object(storage, "copy_tables", side_effect=AssertionError("reimport")):
            self.assert_migrated()
        self.assertFalse((self.runtime / "runtime.sqlite3").exists())
        self.assertFalse((self.control / "control.sqlite3").exists())

    def test_completed_backup_can_restore_v050_before_activation(self):
        self.assert_migrated()
        backup = self.runtime / storage.BACKUP
        restored = self.root / "restored"
        shutil.copytree(backup / "runtime", restored / "runtime")
        shutil.copytree(backup / "control", restored / "runtime-control")
        with closing(self.legacy.Outbox(restored / "runtime", "service", "http://memory.test", credential_fingerprint="test")) as queue:
            self.assertEqual(queue.deployment_id, self.scopes["alice"]["identity_json"].split('"')[1])
            self.assertEqual(queue.get(self.tasks["alice"]["id"])["operation_id"], self.tasks["alice"]["operation_id"])

    def test_incomplete_backup_and_invalid_main_are_never_new_installations(self):
        with patch.object(storage, "copy_tables", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                self.open()
        manifest_path = self.runtime / storage.BACKUP / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"] = {}
        manifest_path.write_text(canonical(manifest))
        with self.assertRaises(QueueError):
            self.open()
        self.assert_originals()
        (self.runtime / "bridge.sqlite3").touch()
        with self.assertRaises(QueueError):
            self.open()
