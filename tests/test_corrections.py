"""Native recall results and safe handoff from the removed correction store."""

from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from plugins.astrbot_plugin_hindsight_bridge.core.materials import memory_block
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox, QueueError


class NativeMemories(unittest.TestCase):
    def test_native_facts_and_observations_keep_service_order(self):
        rows = [
            {"id": "fact", "text": "native corrected fact", "type": "world"},
            {"id": "derived", "text": "derived observation", "type": "observation",
             "source_fact_ids": ["fact"]},
            {"id": "unlinked", "text": "unlinked observation", "type": "observation"},
        ]
        block = memory_block(rows)
        self.assertLess(block.index("native corrected fact"), block.index("derived observation"))
        self.assertLess(block.index("derived observation"), block.index("unlinked observation"))
        self.assertEqual(memory_block([]), "")
        self.assertEqual(memory_block(rows, limit=1), "")
        self.assertLessEqual(len(memory_block(rows, limit=150)), 150)

    def test_assistant_provenance_does_not_claim_execution_or_delivery(self):
        block = memory_block([{
            "id": "reply", "text": "I completed the operation.",
            "document_id": "aw1-0-" + "a" * 64, "type": "experience",
        }])
        self.assertIn("助手回复", block)
        self.assertIn("I completed the operation.", block)
        self.assertNotIn("aw1-", block)

    def test_nonempty_legacy_store_stops_without_deleting_records(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir()
            path = runtime / "runtime.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE corrections(text TEXT)")
                db.execute("INSERT INTO corrections VALUES ('preserve-me')")
            with self.assertRaises(QueueError):
                Outbox(runtime, "test", "http://memory.test", credential_fingerprint="key")
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT text FROM corrections").fetchone()[0],
                                 "preserve-me")

    def test_incomplete_legacy_store_cannot_initialize_empty_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir()
            with closing(sqlite3.connect(runtime / "runtime.sqlite3")) as db, db:
                db.execute("CREATE TABLE corrections(text TEXT)")
            with self.assertRaises(QueueError):
                Outbox(runtime, "test", "http://memory.test", credential_fingerprint="key")
            self.assertFalse((runtime / "bridge.sqlite3").exists())
