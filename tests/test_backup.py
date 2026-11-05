"""Backend version metadata is explicit, not guessed from a deployment default."""

import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("bridge_backup", Path(__file__).parents[1] / "scripts/backup.py")
BACKUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKUP)


class BackupTests(unittest.TestCase):
    def test_unknown_and_explicit_backend_versions_preserve_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, control = root / "data", root / "control"
            for directory, name in ((data, "runtime.sqlite3"), (control, "control.sqlite3")):
                directory.mkdir()
                with sqlite3.connect(directory / name) as db:
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

