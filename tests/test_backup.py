"""Backend version metadata is explicit, not guessed from a deployment default."""

from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from plugins.astrbot_plugin_hindsight_bridge.core.control import Control
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Scope
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox


SPEC = importlib.util.spec_from_file_location("bridge_backup", Path(__file__).parents[1] / "scripts/backup.py")
BACKUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKUP)


class BackupTests(unittest.TestCase):
    def fixture(self, root, *, deployment="fixture", control_id="control", revision=9, request="X"):
        data, control = root / "data", root / "control"
        data.mkdir(parents=True)
        with closing(sqlite3.connect(data / "runtime.sqlite3")) as db, db:
            db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
            db.executemany("INSERT INTO settings VALUES (?,?)", [
                ("deployment_id", deployment), ("control_id", control_id), ("control_revision", str(revision)),
            ])
        with closing(Control(control, deployment)) as journal:
            with journal.transaction():
                journal.db.executemany("UPDATE settings SET value=? WHERE key=?", (
                    (control_id, "control_id"), (str(revision), "revision"),
                ))
            if request is not None:
                self.record(journal, request, dispatched=True)
        return data, control

    def record(self, journal, operation, *, digest="digest", **flags):
        journal.record_request({
            "operation_id": operation, "connection_id": "connection", "bank": "bank", "payload_sha256": digest,
        }, "owner", **flags)

    def control_state(self, path):
        with closing(BACKUP.connect(path)) as db:
            return BACKUP.settings(db)["revision"], db.execute("SELECT operation_id FROM requests").fetchall()

    def test_latest_control_preserves_conflicts_and_previous_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            latest = root / "latest" / "control.sqlite3"
            initial = self.fixture(root / "initial")
            BACKUP.backup(*initial, root / "first", latest)
            BACKUP.backup(*initial, root / "repeat", latest)
            self.assertEqual(list(latest.parent.glob("*.archive-*")), [])
            for name, fields, error in (
                ("foreign", {"deployment": "other"}, "latest_control_identity_mismatch"),
                ("recreated", {"control_id": "other"}, "latest_control_identity_mismatch"),
                ("older", {"revision": 3}, "latest_control_revision_regression"),
                ("fork", {"request": "Y"}, "latest_control_revision_conflict"),
                ("higher-fork", {"revision": 10, "request": None}, "latest_control_evidence_conflict"),
            ):
                output = root / f"output-{name}"
                source = self.fixture(root / name, **fields)
                with self.assertRaisesRegex(ValueError, error):
                    BACKUP.backup(*source, output, latest)
                self.assertFalse(output.exists())
                self.assertEqual(self.control_state(latest), ("9", [("X",)]))
                if name.endswith("fork"):
                    archives = set(latest.parent.glob("*.archive-*/control.sqlite3"))
                    with self.assertRaisesRegex(ValueError, error) as conflict:
                        BACKUP.backup(*source, output, latest)
                    self.assertEqual(set(latest.parent.glob("*.archive-*/control.sqlite3")), archives)
                    self.assertIn(str(latest.resolve()), str(conflict.exception))
                    self.assertTrue(any(str(path.resolve()) in str(conflict.exception) for path in archives))
            BACKUP.backup(*self.fixture(root / "advance", revision=10), root / "higher", latest)
            self.assertEqual(self.control_state(latest), ("10", [("X",)]))
            archives = latest.parent.glob("*.archive-*/control.sqlite3")
            self.assertCountEqual([self.control_state(path) for path in archives], [
                ("9", [("X",)]), ("9", [("Y",)]), ("10", []),
            ])

    def test_delivery_progress_can_publish_over_existing_control_format(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, control = self.fixture(root / "桥接 存档", request=None)
            latest = root / "备份" / "control.sqlite3"
            with closing(sqlite3.connect(control / "control.sqlite3")) as db, db:
                db.execute("DROP TABLE reflection_requests")
                db.execute("PRAGMA user_version=5")
            BACKUP.backup(data, control, root / "original", latest)
            with closing(Control(control, "fixture", required=True)) as journal:
                self.record(journal, "X", dispatched=True)
                BACKUP.backup(data, control, root / "dispatch", latest)
                self.record(journal, "X", accepted=True, cancel=True)
                BACKUP.backup(data, control, root / "complete", latest)
                self.assertEqual(journal.setting("revision"), "9")
            with closing(BACKUP.connect(latest)) as db:
                self.assertEqual(db.execute("SELECT dispatched,accepted,cancel_intent FROM requests").fetchone(), (1, 1, 1))
            self.assertTrue((root / "complete/runtime.sqlite3").is_file())
            for label, digest, flags in (
                ("changed-digest", "other", {"dispatched": True, "accepted": True, "cancel": True}),
                ("lost-acceptance", "digest", {"dispatched": True, "cancel": True}),
            ):
                with self.subTest(label=label):
                    clone_data, clone_control = self.fixture(root / label, revision=10, request=None)
                    with closing(Control(clone_control, "fixture", required=True)) as clone:
                        self.record(clone, "X", digest=digest, **flags)
                    with self.assertRaisesRegex(ValueError, "latest_control_evidence_conflict \\(requests\\)"):
                        BACKUP.backup(clone_data, clone_control, root / f"snapshot-{label}", latest)

    def test_new_connection_migration_marker_can_advance_an_existing_same_revision_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, control = root / "data", root / "control"
            latest = root / "backup" / "control.sqlite3"
            newer = root / "newer" / "control.sqlite3"
            for service, destination in (("first", latest), ("second", newer)):
                with closing(Outbox(data, service, "http://memory.test", credential_fingerprint="fixture",
                                    control_directory=control)) as queue:
                    queue.sync_permissions([])
                    self.assertEqual(queue.control.setting("revision"), "0")
                manifest = BACKUP.backup(data, control, root / service, destination)
                self.assertEqual(manifest["last_seen_control_revision"], 0)

            with closing(BACKUP.connect(latest)) as db:
                old_settings = BACKUP.settings(db)
            with closing(BACKUP.connect(newer)) as db:
                new_settings = BACKUP.settings(db)
            self.assertEqual(old_settings["authorization_migration:first"], "1")
            self.assertEqual(new_settings, {**old_settings, "authorization_migration:second": "1"})
            output = root / "resumed"
            BACKUP.backup(root / "second", newer.parent, output, latest)
            self.assertTrue((output / "runtime.sqlite3").is_file())
            with closing(BACKUP.connect(latest)) as db:
                self.assertEqual(BACKUP.settings(db), new_settings)

            for name, change, revision, error in (
                ("removed", "DELETE FROM settings WHERE key='authorization_migration:first'", 0, "revision"),
                ("rewritten", "UPDATE settings SET value='0' WHERE key='authorization_migration:first'", 1, "evidence"),
                ("invalid", "INSERT INTO settings VALUES ('authorization_migration:third','0')", 0, "revision"),
            ):
                with self.subTest(name=name):
                    fork = root / name
                    fork.mkdir()
                    with closing(BACKUP.connect(newer)) as source:
                        BACKUP.snapshot(source, fork / "control.sqlite3")
                    with closing(sqlite3.connect(fork / "control.sqlite3")) as db, db:
                        db.execute(change)
                        db.execute("UPDATE settings SET value=? WHERE key='revision'", (str(revision),))
                    rejected = root / f"backup-{name}"
                    with self.assertRaisesRegex(ValueError, rf"latest_control_{error}_conflict \(settings\)"):
                        BACKUP.backup(root / "second", fork, rejected, latest)
                    self.assertFalse(rejected.exists())
                    with closing(BACKUP.connect(latest)) as db:
                        self.assertEqual(BACKUP.settings(db), new_settings)

    def test_higher_revision_clone_without_erasure_does_not_replace_latest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, control = self.fixture(root / "original")
            latest = root / "backup" / "control.sqlite3"
            clone = root / "clone"
            clone.mkdir()
            with closing(Control(control, "fixture", required=True)) as journal:
                owner = journal.ensure_account("connection", Scope("fixture", "onebot", "bot", "1", "2"), 0)
                BACKUP.snapshot(journal.db, clone / "control.sqlite3")
                erased = journal.forget("connection", owner["owner_id"])
                BACKUP.backup(data, control, root / "after-forget", latest)
            with closing(Control(clone, "fixture", required=True)) as journal:
                journal.set_mode("connection", owner["owner_id"], "paused")
                journal.set_mode("connection", owner["owner_id"], "active")
                self.assertGreater(int(journal.setting("revision")), 11)
                with self.assertRaisesRegex(ValueError, "latest_control_evidence_conflict \\(erasures\\)"):
                    BACKUP.backup(data, clone, root / "from-clone", latest)
            with closing(BACKUP.connect(latest)) as db:
                self.assertEqual(db.execute("SELECT forget_id,new_bank FROM erasures").fetchone(), (
                    erased["forget_id"], erased["new_bank"],
                ))
            archived, = latest.parent.glob("*.archive-*/control.sqlite3")
            self.assertEqual(self.control_state(archived), ("12", [("X",)]))

    def test_latest_control_failure_keeps_private_snapshot_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            latest, output = root / "latest.sqlite3", root / "output"
            BACKUP.backup(*self.fixture(root / "original"), root / "first", latest)
            fixture = self.fixture(root / "next", revision=10)
            replace = Path.replace

            def fail_publication(path, target):
                if target == latest:
                    if os.name != "nt":
                        staged = next(root.glob("*/business"))
                        self.assertEqual(staged.stat().st_mode & 0o777, 0o700)
                        for saved in staged.iterdir():
                            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
                    raise OSError("publication failed")
                return replace(path, target)

            with patch.object(Path, "replace", fail_publication), self.assertRaisesRegex(OSError, "publication failed"):
                BACKUP.backup(*fixture, output, latest)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob("*/business")), [])
            self.assertEqual(self.control_state(latest), ("9", [("X",)]))
            BACKUP.backup(*fixture, output, latest)
            self.assertEqual(self.control_state(latest), ("10", [("X",)]))

    def test_second_process_cannot_publish_while_latest_control_is_locked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            latest, output = root / "latest.sqlite3", root / "output"
            data, control = self.fixture(root / "fixture")
            command = [sys.executable, str(SPEC.origin), "--data-dir", str(data),
                       "--control-dir", str(control), "--output", str(output), "--latest-control", str(latest)]
            with latest.with_name(latest.name + ".lock").open("a+b") as lock:
                BACKUP.exclusive_lock(lock)
                result = subprocess.run(command, capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("latest_control_lock_failed", result.stderr)
                self.assertFalse(latest.exists())
                self.assertFalse(output.exists())
            subprocess.run(command, check=True, capture_output=True, timeout=15)
            self.assertEqual(self.control_state(latest), ("9", [("X",)]))

    def test_process_locks_and_sqlite_reopen_on_unicode_path(self):
        worker = """
import os
from pathlib import Path
import sys
from core.locking import exclusive_lock

handles = []
for name in sys.argv[1:]:
    try:
        handle = Path(name).open("a+b")
        handles.append(handle)
        exclusive_lock(handle)
    except OSError:
        print("locked", flush=True)
    else:
        print("acquired", flush=True)
os._exit(0)
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "桥接 存档"
            runtime, control = root / "运行", root / "控制"
            command = [sys.executable, "-c", worker, str(runtime / "worker.lock"), str(control / "control.lock")]

            def probe():
                result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], check=True,
                                        capture_output=True, text=True, timeout=15)
                return result.stdout.splitlines()

            def open_queue():
                return Outbox(runtime, "fixture", "http://memory.test",
                              credential_fingerprint="fixture", control_directory=control)

            with closing(open_queue()) as queue:
                deployment = queue.deployment_id
                self.assertEqual(probe(), ["locked", "locked"])
            self.assertEqual(probe(), ["acquired", "acquired"])
            with closing(open_queue()) as queue:
                self.assertEqual(queue.deployment_id, deployment)
                self.assertEqual(queue.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_unknown_and_explicit_backend_versions_preserve_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, control = root / "data", root / "control"
            for directory, name in ((data, "runtime.sqlite3"), (control, "control.sqlite3")):
                directory.mkdir()
                with closing(sqlite3.connect(directory / name)) as db, db:
                    db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
                    db.executemany("INSERT INTO settings VALUES (?,?)", [
                        ("deployment_id", "fixture"), ("control_id", "control"), ("control_revision", "9"),
                    ])
            for version in (None, "1.1.0", "1.2.0"):
                output = root / (version or "unknown")
                arguments = {} if version is None else {"backend_version": version}
                result = BACKUP.backup(data, control, output, **arguments)
                captured = json.loads((output / "manifest.json").read_text())
                self.assertEqual(result, captured)
                self.assertEqual(captured["backend_version"], version)
                self.assertEqual(captured["deployment_id"], "fixture")
                self.assertEqual(captured["last_seen_control_revision"], 9)

