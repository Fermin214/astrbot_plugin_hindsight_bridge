from contextlib import closing
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox


SPEC = importlib.util.spec_from_file_location("bridge_backup", Path(__file__).parents[1] / "scripts/backup.py")
BACKUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKUP)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.queue = Outbox(self.data, "service", "http://memory.test", credential_fingerprint="test")
        self.addCleanup(lambda: self.queue.close())
        self.queue.sync_permissions([("onebot", "alice")])
        incoming = Incoming("aiocqhttp", "onebot", "bot", "alice", "one", int(time.time()) + 1, "正文")
        scope = self.queue.activate(incoming, self.queue.access.authorization("onebot", "alice")["revision"])
        self.capture = self.queue.capture(replace(incoming, received_at=time.time() + 1), "conversation")
        self.turn = self.queue.references.start(scope, "conversation", "one", {"kind": "request", "status": "no_match", "items": []})

    def test_online_snapshot_includes_wal_and_independent_history(self):
        output = self.root / "snapshot"
        manifest = BACKUP.backup(self.data, output, "1.2.0")
        self.assertEqual(manifest["backend_version"], "1.2.0")
        self.assertEqual(manifest["deployment_id"], self.queue.deployment_id)
        self.assertEqual(set(manifest["files"]), {"bridge.sqlite3", "references.sqlite3"})
        with closing(BACKUP.connect(output / "bridge.sqlite3")) as database:
            task = database.execute("SELECT * FROM outbox").fetchone()
            self.assertEqual(task["operation_id"], self.queue.get(self.capture.task_id)["operation_id"])
            self.assertEqual(task["payload"], self.queue.get(self.capture.task_id)["payload"])
        with closing(BACKUP.connect(output / "references.sqlite3")) as database:
            self.assertEqual(database.execute("SELECT id FROM turns").fetchone()[0], self.turn)
        if os.name != "nt":
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            for path in output.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            BACKUP.backup(self.data, output)

    def test_restore_rolls_back_full_main_state_and_history(self):
        output = self.root / "snapshot"
        manifest = BACKUP.backup(self.data, output)
        self.assertIsNone(manifest["backend_version"])
        self.queue.forget(self.capture.scope_id)
        self.queue.close()
        BACKUP.restore(output, self.data)
        self.queue = Outbox(self.data, "service", "http://memory.test", credential_fingerprint="test")
        self.queue.sync_permissions([("onebot", "alice")])
        self.assertEqual(self.queue.scope_owner(self.capture.scope_id)["bank"], self.capture.bank)
        self.assertEqual(self.queue.get(self.capture.task_id)["state"], "ready")
        self.assertFalse(self.queue.control.erasures("service"))
        self.assertEqual(self.queue.references.list(self.queue.scope_owner(self.capture.scope_id))[0]["id"], self.turn)

    def test_active_worker_tampered_manifest_and_snapshot_refuse_restore(self):
        output = self.root / "snapshot"
        manifest = BACKUP.backup(self.data, output)
        with self.assertRaises(OSError):
            BACKUP.restore(output, self.data)
        self.queue.close()
        path = output / "manifest.json"
        for change in ({"deployment_id": "wrong"}, {"files": {"../foreign.sqlite3": "wrong"}}, {"main_schema_version": 0}):
            with self.subTest(change=change):
                path.write_text(json.dumps({**manifest, **change}))
                with self.assertRaises(ValueError):
                    BACKUP.restore(output, self.data)
        path.write_text(json.dumps(manifest))
        with (output / "bridge.sqlite3").open("ab") as database:
            database.write(b"changed")
        with self.assertRaises(ValueError):
            BACKUP.restore(output, self.data)

    def test_failed_main_replacement_preserves_pre_restore_committed_wal(self):
        output = self.root / "snapshot"
        BACKUP.backup(self.data, output)
        self.queue.close()
        database = sqlite3.connect(self.data / "bridge.sqlite3")
        self.addCleanup(database.close)
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA wal_autocheckpoint=0")
        database.execute("INSERT INTO settings VALUES ('restore-interruption','committed')")
        database.commit()
        replace_file = os.replace

        def interrupted(source, target):
            if Path(target).name == "bridge.sqlite3":
                raise OSError("injected replacement failure")
            return replace_file(source, target)

        with patch.object(BACKUP.os, "replace", side_effect=interrupted), self.assertRaises(OSError):
            BACKUP.restore(output, self.data)
        database.close()
        with closing(BACKUP.connect(self.data / "bridge.sqlite3")) as restored:
            self.assertEqual(restored.execute("SELECT value FROM settings WHERE key='restore-interruption'").fetchone()[0], "committed")

    def test_missing_main_nested_output_and_invalid_database_refuse_backup(self):
        with self.assertRaises(ValueError):
            BACKUP.backup(self.data, self.data / "nested")
        with self.assertRaises(sqlite3.OperationalError):
            BACKUP.backup(self.root / "missing", self.root / "snapshot")
        self.assertFalse((self.root / "snapshot").exists())
        self.queue.close()
        with closing(sqlite3.connect(self.data / "bridge.sqlite3")) as database, database:
            database.execute("DELETE FROM settings WHERE key='storage_format'")
        with self.assertRaises(ValueError):
            BACKUP.backup(self.data, self.root / "snapshot")
        self.assertFalse((self.root / "snapshot").exists())
