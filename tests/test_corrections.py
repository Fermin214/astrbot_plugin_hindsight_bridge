"""Correction provenance and erase/restore invariants that need deterministic faults."""

import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming, sha256
from plugins.astrbot_plugin_hindsight_bridge.core.materials import memory_block
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox, QueueError, SCHEMA_VERSION


class Corrections(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.open()
        self.source = Incoming(
            "aiocqhttp",
            "onebot",
            "bot",
            "alice",
            "correct-1",
            int(time.time()),
            "周五改成下午去看展，不再是上午。",
        )
        self.target = {
            "id": "fact-1",
            "text": "用户计划周五上午看展。",
            "document_id": "source-1",
        }

    def open(self):
        self.queue = Outbox(
            self.root / "runtime",
            "test",
            "http://memory.test",
            credential_fingerprint=sha256("key"),
            control_directory=self.root / "control",
        )

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def capture(self, source=None, target=None):
        return self.queue.capture(
            source or self.source,
            "manual-corrections",
            correction=target or self.target,
        )

    def test_frozen_correction_and_same_event_different_target_conflict(self):
        captured = self.capture()
        row = self.queue.get(captured.task_id)
        payload = self.queue.verify(row)
        metadata = payload["items"][0]["metadata"]
        self.assertEqual(metadata["format"], "user_correction_v1")
        self.assertEqual(metadata["corrects_memory_id"], "fact-1")
        self.assertEqual(metadata["corrects_document_id"], "source-1")
        self.assertFalse(self.capture().created)
        with self.assertRaisesRegex(QueueError, "message_identity_content_conflict"):
            self.capture(target={**self.target, "id": "fact-2"})
        self.queue.close()
        self.open()
        self.assertEqual(self.queue.verify(self.queue.get(captured.task_id)), payload)
        self.assertEqual(
            self.queue.get(captured.task_id)["operation_id"], row["operation_id"]
        )

    def test_completed_queue_keeps_local_correction_and_isolates_users(self):
        captured = self.capture()
        self.queue.update(captured.task_id, "done")
        self.assertEqual(self.queue.get(captured.task_id)["payload"], "")
        alice = self.queue.scope(self.source)
        bob = self.queue.scope(replace(self.source, user_id="bob"))
        self.assertEqual(self.queue.corrections(bob), [])
        changes = self.queue.corrections(alice)
        self.assertEqual(changes[0]["text"], self.source.text)
        block = memory_block(
            [{"id": "fact-1", "text": self.target["text"], "type": "world"}],
            corrections=changes,
        )
        self.assertIn(self.source.text, block)
        self.assertIn("用户显式更正", block)
        self.assertNotIn('"text": "用户计划周五上午看展。"', block)
        self.assertLessEqual(len(block), 4000)

    def test_erase_and_old_business_restore_remove_correction_text(self):
        captured = self.capture()
        snapshot = self.root / "old.sqlite3"
        with closing(sqlite3.connect(snapshot)) as copy:
            self.queue.db.backup(copy)
        self.queue.forget(captured.scope_id)
        self.assertEqual(
            self.queue.db.execute("SELECT COUNT(*) FROM corrections").fetchone()[0], 0
        )
        self.queue.close()
        with (
            closing(sqlite3.connect(snapshot)) as old,
            closing(
                sqlite3.connect(self.root / "runtime" / "runtime.sqlite3")
            ) as target,
        ):
            old.backup(target)
        self.open()
        self.assertEqual(self.queue.get(captured.task_id)["state"], "discarded")
        self.assertEqual(
            self.queue.db.execute("SELECT COUNT(*) FROM corrections").fetchone()[0], 0
        )
        self.assertNotIn(self.source.text, "\n".join(self.queue.db.iterdump()))
        self.assertNotIn(self.source.text, "\n".join(self.queue.control.db.iterdump()))

    def test_capture_capacity_rolls_back_correction_and_mode_requires_active(self):
        self.queue.max_bytes = 1
        with self.assertRaisesRegex(QueueError, "queue_capacity_reached"):
            self.capture()
        self.assertEqual(
            self.queue.db.execute("SELECT COUNT(*) FROM corrections").fetchone()[0], 0
        )
        self.queue.max_bytes = 64 * 1024 * 1024
        scope = self.queue.scope(self.source)
        self.queue.set_mode(scope["scope_id"], "readonly")
        with self.assertRaisesRegex(QueueError, "scope_not_active"):
            self.capture()

    def test_newer_correction_supersedes_local_overlay_and_keeps_provenance(self):
        first = self.capture()
        second = self.capture(
            replace(self.source, message_id="correct-2", text="周五取消看展。")
        )
        changes = self.queue.corrections(self.queue.scope(self.source))
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["task_id"], second.task_id)
        self.assertEqual(
            self.queue.verify(self.queue.get(first.task_id))["items"][0]["metadata"][
                "corrects_memory_id"
            ],
            "fact-1",
        )
        self.assertEqual(
            self.queue.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
        )

    def test_administrator_correction_retains_provenance_after_completion_and_restart(self):
        captured = self.capture(target={
            **self.target, "source": "administrator", "submitted_by": "admin",
        })
        item = self.queue.verify(self.queue.get(captured.task_id))["items"][0]
        self.assertEqual(item["metadata"]["role"], "administrator")
        self.assertEqual(item["metadata"]["format"], "administrator_correction_v1")
        self.assertTrue(item["document_id"].startswith("ac1-"))
        self.assertIn("不是用户私聊原文", item["content"])
        self.queue.update(captured.task_id, "done")
        self.queue.close()
        self.open()
        changes = self.queue.corrections(self.queue.scope(self.source))
        self.assertEqual(changes[0]["source"], "administrator")
        self.assertEqual(changes[0]["submitted_by"], "admin")
        block = memory_block([], corrections=changes)
        self.assertIn("管理员更正", block)
        self.assertNotIn("用户更正", block)
        self.queue.forget(captured.scope_id)
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM corrections").fetchone()[0], 0)

    def test_schema_four_migration_preserves_user_correction_and_frozen_request(self):
        captured = self.capture()
        frozen = self.queue.verify(self.queue.get(captured.task_id))
        self.queue.db.execute("ALTER TABLE corrections DROP COLUMN submitted_by")
        self.queue.db.execute("ALTER TABLE corrections DROP COLUMN source")
        self.queue.db.execute("PRAGMA user_version=4")
        self.queue.db.commit()
        self.queue.close()
        self.open()
        self.assertEqual(self.queue.verify(self.queue.get(captured.task_id)), frozen)
        changes = self.queue.corrections(self.queue.scope(self.source))
        self.assertEqual(changes[0]["source"], "user")
        self.assertEqual(changes[0]["submitted_by"], "")

    def test_derived_observations_do_not_restore_corrected_values(self):
        changes = [
            {
                "memory_id": f"f{index}",
                "original_text": "旧" * 595,
                "text": str(index) * 995,
                "occurred_at": "2026-10-24",
            }
            for index in range(3)
        ]
        facts = [
            {"id": f"f{index}", "text": f"old-fact-{index}", "type": "world"}
            for index in range(3)
        ]
        for key in ("source_memory_ids", "source_fact_ids"):
            with self.subTest(key=key):
                block = memory_block(
                    [
                        *facts,
                        {
                            "id": "o0",
                            "type": "observation",
                            "text": "outdated-derived-value",
                            key: ["f0"],
                        },
                        {
                            "id": "o1",
                            "type": "observation",
                            "text": "unknown-provenance",
                        },
                    ],
                    corrections=changes,
                )
                self.assertNotIn("outdated-derived-value", block)
                self.assertNotIn("old-fact", block)
                self.assertNotIn("unknown-provenance", block)
                self.assertLessEqual(len(block), 4000)

    def test_strict_budget_and_unfit_correction_omit_old_material(self):
        change = {
            "memory_id": "f",
            "original_text": "x" * 90,
            "text": "y" * 30,
            "occurred_at": "2026-10-24",
        }
        self.assertLessEqual(
            len(memory_block([], limit=256, corrections=[change])), 256
        )
        block = memory_block(
            [{"id": "old", "text": "obsolete", "type": "world"}],
            limit=256,
            corrections=[{**change, "text": "new-value" * 125}],
        )
        self.assertNotIn("obsolete", block)
        self.assertNotIn("new-value", block)
        self.assertIn("更正材料省略", block)
        self.assertLessEqual(len(block), 256)


if __name__ == "__main__":
    unittest.main()
