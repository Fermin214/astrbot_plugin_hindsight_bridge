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

from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox


SPEC = importlib.util.spec_from_file_location("bridge_backup", Path(__file__).parents[1] / "scripts/backup.py")
BACKUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKUP)


class BackupTests(unittest.TestCase):
    def fixture(self, root, *, deployment="fixture", control_id="control", revision=9, request="X"):
        data, control = root / "data", root / "control"
        for directory, name in ((data, "runtime.sqlite3"), (control, "control.sqlite3")):
            directory.mkdir(parents=True)
            with closing(sqlite3.connect(directory / name)) as db, db:
                db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
                db.executemany("INSERT INTO settings VALUES (?,?)", [
                    ("deployment_id", deployment), ("control_id", control_id),
                    ("control_revision" if directory == data else "revision", str(revision)),
                ])
                if directory == control:
                    db.execute("CREATE TABLE requests (id TEXT PRIMARY KEY)")
                    if request is not None:
                        db.execute("INSERT INTO requests VALUES (?)", (request,))
        return data, control

    def control_state(self, path):
        with closing(BACKUP.connect(path)) as db:
            return BACKUP.settings(db)["revision"], db.execute("SELECT id FROM requests").fetchall()

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
            ):
                output = root / ("output-" + name)
                with self.assertRaisesRegex(ValueError, error):
                    BACKUP.backup(*self.fixture(root / name, **fields), output, latest)
                self.assertFalse(output.exists())
                self.assertEqual(self.control_state(latest), ("9", [("X",)]))
            BACKUP.backup(*self.fixture(root / "clone", revision=10, request=None), root / "higher", latest)
            self.assertEqual(self.control_state(latest), ("10", []))
            archives = latest.parent.glob("*.archive-*/control.sqlite3")
            self.assertCountEqual([self.control_state(path) for path in archives], [
                ("9", [("X",)]), ("9", [("Y",)]),
            ])

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

