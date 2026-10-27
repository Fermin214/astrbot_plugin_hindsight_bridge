"""Native recall results and safe handoff from the removed correction store."""

import json
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
        payload = json.loads(block[block.index("{"):])
        self.assertEqual(list(payload.values()), [rows])
        self.assertEqual(memory_block([]), "")
        self.assertEqual(memory_block(rows, limit=1), "")
        self.assertLessEqual(len(memory_block(rows, limit=150)), 150)

    def test_assistant_provenance_does_not_claim_execution_or_delivery(self):
        block = memory_block([{
            "id": "reply", "text": "I completed the operation.",
            "document_id": "aw1-example", "type": "experience",
        }])
        payload = json.loads(block[block.index("{"):])
        record = list(payload.values())[0][0]
        self.assertIn("assistant", record.values())
        self.assertIn("unknown", record.values())

    def test_nonempty_legacy_store_stops_without_deleting_records(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir()
            path = runtime / "runtime.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE corrections(text TEXT)")
                db.execute("INSERT INTO corrections VALUES ('preserve-me')")
            with self.assertRaisesRegex(QueueError, "legacy_corrections_require_handover"):
                Outbox(runtime, "test", "http://memory.test", credential_fingerprint="key")
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT text FROM corrections").fetchone()[0],
                                 "preserve-me")

    def test_empty_legacy_store_needs_no_migration_framework(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir()
            with sqlite3.connect(runtime / "runtime.sqlite3") as db:
                db.execute("CREATE TABLE corrections(text TEXT)")
            queue = Outbox(runtime, "test", "http://memory.test", credential_fingerprint="key")
            try:
                self.assertIsNone(queue.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='corrections'"
                ).fetchone())
            finally:
                queue.close()
