"""Backend version metadata is explicit, not guessed from a deployment default."""

from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox


SPEC = importlib.util.spec_from_file_location("bridge_backup", Path(__file__).parents[1] / "scripts/backup.py")
BACKUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKUP)


class BackupTests(unittest.TestCase):
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

