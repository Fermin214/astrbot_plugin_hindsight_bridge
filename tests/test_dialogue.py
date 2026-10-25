"""Persisted-history boundaries and dialogue failures beyond a live chat check."""

import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from plugins.astrbot_plugin_hindsight_bridge.dialogue import (
    appended_history,
    final_reply,
    history_boundary,
    history_messages,
    persistent_messages,
    plain_text,
)
from plugins.astrbot_plugin_hindsight_bridge.identity import Incoming
from plugins.astrbot_plugin_hindsight_bridge.outbox import Outbox, QueueError


class HistoryBoundaries(unittest.TestCase):
    def test_final_text_ignores_tools_reasoning_and_checkpoints(self):
        rows = [
            {"role": "system", "content": "人格与系统提示"},
            {"role": "user", "content": "安排一下周末"},
            {"role": "assistant", "content": "工具中间说明", "tool_calls": [{}]},
            {"role": "tool", "content": "不作为对话正文的结果"},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "周六看展，或者周日爬山。"}],
                "reasoning_content": "内部推理不得进入记忆",
            },
            {"role": "_checkpoint", "content": {"id": "private"}},
        ]
        stored = SimpleNamespace(history=json.dumps(rows, ensure_ascii=False))
        self.assertEqual(
            final_reply(history_messages(stored), "安排一下周末"),
            "周六看展，或者周日爬山。",
        )
        self.assertIsNone(final_reply(rows, "另一个用户的问题"))
        self.assertIsNone(plain_text(rows[2]))
        self.assertIsNone(plain_text(rows[3]))
        self.assertIsNone(
            plain_text(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "召回块", "_no_save": True}],
                }
            )
        )
        self.assertIsNone(plain_text({"role": "assistant", "content": "<think>思考"}))
        self.assertIsNone(history_messages(SimpleNamespace(history="not json")))

    def test_last_turn_only_and_incomplete_generation_are_not_backfilled(self):
        rows = [
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "旧回复"},
            {"role": "user", "content": "新问题"},
            {"role": "assistant", "content": None, "tool_calls": [{}]},
        ]
        self.assertIsNone(final_reply(rows, "旧问题"))
        self.assertIsNone(final_reply(rows, "新问题"))

    def test_failed_read_repeated_question_and_edited_prefix_cannot_reuse_old_reply(
        self,
    ):
        old = [
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "旧回复"},
        ]
        self.assertIsNone(appended_history(old, history_boundary(None)))
        boundary = history_boundary(old)
        self.assertIsNone(appended_history(old, boundary))
        added = [
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "新回复"},
        ]
        self.assertEqual(
            final_reply(appended_history(old + added, boundary), "Q"), "新回复"
        )
        edited = [{"role": "user", "content": "压缩摘要"}, old[-1]]
        self.assertIsNone(appended_history(edited + added, boundary))
        self.assertEqual(appended_history(added, history_boundary([])), added)

    def test_temporary_private_content_is_filtered_before_serialization(self):
        class PrivatePart:
            def __init__(self, text, temporary=False):
                self.text, self._no_save = text, temporary

            def model_dump(self):
                return {"type": "text", "text": self.text}

        class RuntimeMessage:
            _no_save = False

            def __init__(self, parts):
                self.content = parts

            def model_dump(self):
                return {
                    "role": "user",
                    "content": [part.model_dump() for part in self.content],
                }

        user = RuntimeMessage(
            [PrivatePart("Q"), PrivatePart("临时召回块", temporary=True)]
        )
        rows = persistent_messages([user, {"role": "assistant", "content": "A"}])
        self.assertEqual(final_reply(rows, "Q"), "A")
        self.assertNotIn("临时召回块", json.dumps(rows, ensure_ascii=False))


class DialogueDurability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / "runtime"
        self.open()
        self.source = Incoming(
            "aiocqhttp",
            "onebot",
            "bot",
            "alice",
            "turn-1",
            int(time.time()),
            "周末怎么安排",
        )

    def open(self):
        self.queue = Outbox(
            self.directory,
            "service",
            "http://memory.test",
            credential_fingerprint="key",
        )

    def tearDown(self):
        self.queue.close()
        self.tmp.cleanup()

    def test_assistant_freeze_survives_retry_and_supplies_next_user_context(self):
        user = self.queue.capture(self.source, "chat")
        original = self.queue.get(user.task_id)["payload"]
        assistant = self.queue.capture_assistant(
            user.task_id, "第一种周六看展，第二种周日爬山。"
        )
        task = dict(self.queue.get(assistant.task_id))
        item = json.loads(task["payload"])["items"][0]
        self.assertEqual(item["metadata"]["role"], "assistant")
        self.assertEqual(item["metadata"]["delivery"], "unknown")
        self.assertEqual(item["metadata"]["time_source"], "history_observed_at")
        self.assertTrue(item["document_id"].startswith("aw1-0-"))
        self.assertIn("不能证明操作实际执行", item["content"])
        self.assertEqual(self.queue.get(user.task_id)["payload"], original)
        self.assertFalse(
            self.queue.capture_assistant(
                user.task_id, "第一种周六看展，第二种周日爬山。"
            ).created
        )
        changed = Incoming(
            *(
                self.source.platform_type,
                self.source.platform_instance_id,
                self.source.bot_id,
                self.source.user_id,
                "turn-2",
                self.source.timestamp,
                "先按第二个。",
            )
        )
        decision = self.queue.capture(changed, "chat")
        self.assertEqual([m.role for m in decision.history], ["user", "assistant"])
        body = json.loads(self.queue.get(decision.task_id)["payload"])["items"][0]
        self.assertEqual(body["metadata"]["format"], "dialogue_window_v2")
        self.assertIn("第二种周日爬山", body["content"])
        self.queue.close()
        self.open()
        self.assertEqual(
            self.queue.verify(self.queue.get(assistant.task_id)),
            json.loads(task["payload"]),
        )
        self.assertEqual(
            self.queue.get(assistant.task_id)["operation_id"], task["operation_id"]
        )

    def test_pause_resume_and_permission_changes_drop_late_assistant(self):
        for mode in ("readonly", "paused"):
            user = self.queue.capture(self.source, "chat")
            self.queue.set_mode(user.scope_id, mode)
            self.queue.set_mode(user.scope_id, "active", now=time.time() - 1)
            with self.assertRaisesRegex(QueueError, "scope_not_active"):
                self.queue.capture_assistant(user.task_id, "迟到的承诺")
            self.source = Incoming(
                "aiocqhttp", "onebot", "bot", "alice", mode, int(time.time()), mode
            )
        user = self.queue.capture(self.source, "chat")
        self.queue.capture_assistant(user.task_id, "权限移除前的回复")
        self.source = Incoming(
            "aiocqhttp", "onebot", "bot", "alice", "pending", int(time.time()), "未完成"
        )
        pending = self.queue.capture(
            self.source, "chat", history_before=history_boundary([])
        )
        self.queue.sync_permissions([])
        with self.assertRaisesRegex(QueueError, "scope_not_active"):
            self.queue.capture_assistant(pending.task_id, "权限移除后的回复")
        self.queue.sync_permissions([("onebot", "alice")])
        with self.assertRaisesRegex(QueueError, "scope_not_active"):
            self.queue.capture_assistant(pending.task_id, "恢复权限后的迟到回复")
        self.assertEqual(
            self.queue.db.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 0
        )
        self.assertEqual(
            self.queue.db.execute("SELECT COUNT(*) FROM history_baselines").fetchone()[
                0
            ],
            0,
        )
        fresh = Incoming(
            "aiocqhttp", "onebot", "bot", "alice", "fresh", int(time.time()), "新语境"
        )
        capture = self.queue.capture(fresh, "chat")
        self.assertEqual(capture.history, ())
        self.assertIsNone(
            self.queue.history_anchor(capture.scope_id, "chat")["history_before"]
        )

    def test_capacity_failure_is_atomic_and_history_edit_cannot_change_frozen_task(
        self,
    ):
        user = self.queue.capture(self.source, "chat")
        self.queue.max_tasks = 1
        with self.assertRaisesRegex(QueueError, "queue_capacity_reached"):
            self.queue.capture_assistant(user.task_id, "无法入队")
        self.assertEqual(
            self.queue.db.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 0
        )
        self.assertEqual(
            self.queue.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1
        )
        self.queue.max_tasks = 10
        self.queue.capture_assistant(user.task_id, "保存的最终正文")
        with self.assertRaisesRegex(QueueError, "assistant_history_changed"):
            self.queue.capture_assistant(user.task_id, "后来编辑的正文")

    def test_forget_and_old_snapshot_restore_remove_assistant_text(self):
        user = self.queue.capture(self.source, "chat")
        self.queue.capture_assistant(user.task_id, "不可复活的私人助手正文")
        snapshot = Path(self.tmp.name) / "before.sqlite3"
        backup = sqlite3.connect(snapshot)
        try:
            self.queue.db.backup(backup)
        finally:
            backup.close()
        self.queue.forget(user.scope_id)
        self.assertEqual(
            self.queue.db.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 0
        )
        self.queue.close()
        shutil.copyfile(snapshot, self.directory / "runtime.sqlite3")
        self.open()
        self.assertEqual(
            self.queue.db.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 0
        )
        self.assertTrue(
            all(
                not row[0]
                for row in self.queue.db.execute("SELECT payload FROM outbox")
            )
        )
