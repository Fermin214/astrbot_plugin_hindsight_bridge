import json
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from plugins.astrbot_plugin_hindsight_bridge.core.bank_stats import BankStats
from plugins.astrbot_plugin_hindsight_bridge.core.client import HindsightError
from plugins.astrbot_plugin_hindsight_bridge.core.dialogue import final_reply
from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming
from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox
from plugins.astrbot_plugin_hindsight_bridge.core.panel_scopes import PanelScopes
from plugins.astrbot_plugin_hindsight_bridge.core.reference import reference_options, save_reference_options
from plugins.astrbot_plugin_hindsight_bridge.core.source import extract_segments, message_media, native_header
from plugins.astrbot_plugin_hindsight_bridge.core.telegram_source import text_segments
from queue_fixture import confirm_sources


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    pass


class Face:
    pass


class Reply:
    def __init__(self):
        self.id = "untrusted-id"
        self.text = "untrusted quoted body"


class Gateway(unittest.TestCase):
    def test_skip_media_projection_handles_malformed_collections_and_rich_posts(self):
        for platform, raw in (("aiocqhttp", {"message": None}), ("weixin_oc", {"item_list": True}),
                              ("qq_official", SimpleNamespace(raw_data={"attachments": 42}))):
            event = SimpleNamespace(get_platform_name=lambda: platform, message_obj=SimpleNamespace(raw_message=raw))
            self.assertEqual(message_media(event), ())
        event = SimpleNamespace(get_platform_name=lambda: "lark", message_obj=SimpleNamespace(raw_message=SimpleNamespace(
            message_type="post", content=json.dumps({"content": [[{"tag": "img"}], [{"tag": "media"}]]}),
        )))
        self.assertEqual(message_media(event), ("image", "video"))

    def event(self, segments, parts, visible):
        return SimpleNamespace(
            get_platform_name=lambda: "aiocqhttp", get_platform_id=lambda: "onebot",
            get_self_id=lambda: "1001", get_sender_id=lambda: "2001",
            is_private_chat=lambda: True, get_group_id=lambda: "",
            get_messages=lambda: parts, get_message_str=lambda: visible, created_at=time.time(),
            message_obj=SimpleNamespace(message_id="12", raw_message={
                "post_type": "message", "message_type": "private", "self_id": 1001,
                "user_id": 2001, "message_id": 12, "time": int(time.time()), "message": segments,
            }),
        )

    def test_segment_extraction_preserves_emoji_and_only_skips_media_without_text(self):
        for kind, marker in (("image", "[图片]"), ("mface", "[表情包]"), ("voice", "[语音]"),
                             ("file", "[文件]"), ("video", "[视频]")):
            with self.subTest(kind=kind):
                self.assertEqual(extract_segments([("text", "你好😀"), (kind, None)])[0], "你好😀" + marker)
                self.assertIsNone(extract_segments([("text", "  "), (kind, None)]))
        self.assertEqual(extract_segments([("emoji", "微笑")])[0], "[表情:微笑]")
        self.assertEqual(extract_segments([("emoji", None)])[0], "[表情]")

    def test_onebot_mixed_message_retains_verified_host_text_and_raw_reply_id(self):
        segments = [{"type": "reply", "data": {"id": "8"}},
                    {"type": "text", "data": {"text": "  好的😀  "}},
                    {"type": "face", "data": {"id": "14", "name": "微笑"}},
                    {"type": "image", "data": {"file": "synthetic.png"}},
                    {"type": "mface", "data": {"summary": "untrusted media description"}}]
        event = self.event(segments, [Reply(), Plain("好的😀"), Face(), Image()], "好的😀")
        incoming = native_header(event, Plain)
        self.assertEqual(incoming.text, "  好的😀  [表情:微笑][图片][表情包]")
        self.assertEqual(incoming.host_text, "好的😀")
        self.assertEqual(incoming.reply_to, "8")
        self.assertTrue(incoming.capture_allowed)
        self.assertNotIn("untrusted", incoming.text)
        event.get_messages = lambda: [Plain("forged text"), Image()]
        self.assertIsNone(native_header(event, Plain))

    def test_onebot_pure_media_and_foreign_private_boundaries_are_rejected(self):
        event = self.event([{"type": "image", "data": {"file": "synthetic.png"}}], [Image()], "")
        self.assertIsNone(native_header(event, Plain))
        event = self.event([{"type": "text", "data": {"text": "body"}}], [Plain("body")], "body")
        event.message_obj.raw_message["message_type"] = "group"
        self.assertIsNone(native_header(event, Plain))
        event.message_obj.raw_message["message_type"] = "private"
        event.message_obj.raw_message["self_id"] = 1002
        self.assertIsNone(native_header(event, Plain))

    def test_wake_prefix_mixed_message_keeps_original_text_and_matches_actual_host_history(self):
        event = self.event([{"type": "text", "data": {"text": "小桥 看这张图"}},
                            {"type": "image", "data": {"file": "synthetic.png"}}],
                           [Plain("小桥 看这张图"), Image()], "看这张图")
        event.is_at_or_wake_command = True
        incoming = native_header(event, Plain, ["小桥"])
        self.assertEqual(incoming.text, "小桥 看这张图[图片]")
        self.assertEqual(incoming.host_text, "看这张图")
        self.assertEqual(final_reply([
            {"role": "user", "content": [{"type": "text", "text": "看这张图"},
                                         {"type": "image_url", "image_url": {"url": "local-image"}}]},
            {"role": "assistant", "content": "完整回答"},
        ], incoming.host_text, incoming.media_types), "完整回答")

    def test_native_image_history_placeholders_need_verified_media_and_cannot_hide_extra_text(self):
        content = [{"type": "text", "text": "正文"},
                   {"type": "text", "text": "[Image Attachment: original path /tmp/native.png; not included in this request]"},
                   {"type": "text", "text": "[Image]"}]
        rows = [{"role": "user", "content": content}, {"role": "assistant", "content": "助手完整回复"}]
        self.assertEqual(final_reply(rows, "正文", ("image",)), "助手完整回复")
        self.assertIsNone(final_reply(rows, "正文"))
        content.append({"type": "text", "text": "不能丢弃的额外原话"})
        self.assertIsNone(final_reply(rows, "正文", ("image",)))

    def test_telegram_utf16_entities_preserve_custom_emoji_and_real_link(self):
        entities = [SimpleNamespace(type="custom_emoji", offset=0, length=2, custom_emoji_id="123"),
                    SimpleNamespace(type="text_link", offset=3, length=2, url="https://example.invalid")]
        self.assertEqual(extract_segments(text_segments("😀 看看", entities))[0],
                         "[表情:😀] 看看 (https://example.invalid)")
        entities[0].length = 1
        self.assertIsNone(text_segments("😀 看看", entities))

    def test_telegram_link_can_contain_a_custom_emoji(self):
        entities = [SimpleNamespace(type="text_link", offset=0, length=4, url="https://example.invalid"),
                    SimpleNamespace(type="custom_emoji", offset=1, length=2, custom_emoji_id="123")]
        self.assertEqual(extract_segments(text_segments("看😀呀", entities))[0],
                         "看[表情:😀]呀 (https://example.invalid)")

    def test_multimodal_user_history_only_links_a_pure_text_assistant(self):
        rows = [{"role": "user", "content": [{"type": "text", "text": "看这张图"},
                                               {"type": "image_url", "image_url": {"url": "local-image"}}]},
                {"role": "assistant", "content": [{"type": "text", "text": "完整回答"}]}]
        self.assertEqual(final_reply(rows, "看这张图"), "完整回答")
        self.assertIsNone(final_reply(rows, "另一个问题"))
        rows[-1]["content"].append({"type": "image_url", "image_url": {"url": "assistant-image"}})
        self.assertIsNone(final_reply(rows, "看这张图"))


class RetentionAndRecords(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.queue = Outbox(Path(self.temporary.name) / "runtime", "service", "http://memory.test", credential_fingerprint="key")
        self.addCleanup(self.queue.close)
        confirm_sources(self.queue, ("qq", "bot", "alice"), ("qq", "bot", "bob"))
        self.incoming = Incoming("aiocqhttp", "qq", "bot", "alice", "one", int(time.time()) + 1, "原来的问题")
        self.alice = self.queue.scope(self.incoming)
        self.bob = self.queue.scope(replace(self.incoming, user_id="bob"))

    def test_quote_uses_only_current_local_window_and_excludes_assistant_when_disabled(self):
        original = self.queue.capture(replace(self.incoming, text="用户背景" * 100), "chat")
        self.queue.capture_assistant(original.task_id, "ASSISTANT_PRIVATE")
        quoted = self.queue.capture(replace(self.incoming, message_id="two", timestamp=self.incoming.timestamp + 1, text="那就这么办", reply_to="one"),
                                    "chat", include_assistant_context=False)
        content = json.loads(self.queue.get(quoted.task_id)["payload"])["items"][0]["content"]
        self.assertNotIn("ASSISTANT_PRIVATE", content)
        background = content.split("【引用背景（仅用于理解指代，勿重复抽取）】\n")[1].split("\n【本次新增用户消息】")[0]
        self.assertEqual(len(background), 200)
        foreign = self.queue.capture(replace(self.incoming, user_id="bob", message_id="three", timestamp=self.incoming.timestamp + 2,
                                            reply_to="one", text="用户的新话"), "chat")
        self.assertNotIn("【引用背景", self.queue.get(foreign.task_id)["payload"])
        self.assertNotIn("用户背景", self.queue.get(foreign.task_id)["payload"])

    def test_history_anchor_uses_host_text_without_media_markers(self):
        captured = self.queue.capture(replace(self.incoming, text="正文[图片]", host_text="正文"), "chat")
        anchor = self.queue.history_anchor(captured.scope_id, "chat")
        self.assertEqual(anchor["host_text"], "正文")
        self.assertEqual(json.loads(self.queue.get(captured.task_id)["payload"])["items"][0]["content"].count("切勿凭空臆测媒体内容"), 1)

    def test_skip_retention_deduplicates_and_erases_all_bindings_without_chat_preview(self):
        records = self.queue.references
        for index in range(25):
            self.assertTrue(records.record_skip(self.alice, str(index), "media_or_non_text", ("image",)))
        self.assertFalse(records.record_skip(self.alice, "0", "media_or_non_text", ("image",)))
        self.assertEqual(len(records.skips(self.alice)), 20)
        self.assertEqual(set(records.skips(self.alice)[0]), {"at", "code", "media_types"})
        records.clear_skips(self.alice)
        self.assertEqual(records.skips(self.alice), [])
        self.assertFalse(records.record_skip(self.alice, "0", "media_or_non_text", ("image",)))
        self.assertTrue(records.record_skip(self.alice, "new", "media_or_non_text", ("image",)))
        self.assertTrue(records.record_skip(self.bob, "keep", "command_prefix"))
        self.queue.forget(self.alice["scope_id"])
        self.assertTrue(records.reconcile(force=True))
        self.assertEqual(len(records.skips(self.bob)), 1)
        self.assertEqual(records.db.execute("SELECT COUNT(*) FROM skips").fetchone()[0], 1)
        self.assertEqual(records.db.execute("SELECT COUNT(*) FROM skip_seen").fetchone()[0], 1)

    def test_zero_history_clears_and_stops_skips_and_recent_retrieval(self):
        records = self.queue.references
        records.record_skip(self.alice, "one", "media_or_non_text", ("image",))
        records.start(self.alice, "chat", "one", {"kind": "request", "status": "no_match"})
        save_reference_options(self.queue, self.alice, **{**reference_options(self.queue, self.alice), "history_limit": 0})
        self.assertEqual(records.skips(self.alice), [])
        self.assertFalse(records.record_skip(self.alice, "two", "media_or_non_text", ("image",)))
        self.assertIsNone(records.latest(self.alice))

    def test_recent_retrieval_preserves_actual_delivery_types(self):
        records = self.queue.references
        for delivery, expected in (({"status": "not_called"}, "on_demand"),
                                   ({"status": "no_match"}, "empty"),
                                   ({"status": "error", "code": "recall_timeout"}, "error"),
                                   ({"status": "no_match", "pages": [{"page_id": "page", "content": "page body"}]}, "background_only"),
                                   ({"status": "delivered", "items": [{"text": "fact"}]}, "success")):
            with self.subTest(expected=expected):
                records.start(self.alice, "chat", "one", {"kind": "request", **delivery})
                records.close()
                self.assertEqual(records.latest(self.alice)["outcome"], expected)

    def test_failed_page_then_successful_metadata_read_does_not_claim_page_delivery(self):
        records = self.queue.references
        identifier = records.start(self.alice, "chat", "one", {"kind": "request", "status": "not_called"})
        records.append(self.alice, identifier, {"kind": "read_page", "status": "error", "code": "tool_read_timeout", "pages": [{}]})
        records.append(self.alice, identifier, {"kind": "search_pages", "status": "delivered"})
        self.assertEqual(records.latest(self.alice)["outcome"], "error")
        self.assertEqual(records.latest(self.alice)["code"], "tool_read_timeout")


class RemoteTotals(unittest.IsolatedAsyncioTestCase):
    setUp = RetentionAndRecords.setUp

    async def asyncSetUp(self):
        @asynccontextmanager
        async def resources():
            yield
        self.client = SimpleNamespace(stats=AsyncMock(return_value={"total_documents": 0, "total_nodes": 0}))
        self.plugin = SimpleNamespace(outbox=self.queue, client=self.client, using_resources=resources)
        self.cache = BankStats(self.plugin)

    async def asyncTearDown(self):
        await self.cache.close()

    async def test_only_new_inbound_enqueue_resets_warning_not_a_late_assistant(self):
        self.plugin.config = {"allowed_accounts": [{"platform_instance_id": self.incoming.platform_instance_id, "user_id": user}
                                                 for user in ("alice", "bob")]}
        self.plugin.stopping = False
        self.plugin.refresh_permissions = lambda: None
        self.plugin.recall_status = lambda scope: None
        self.plugin.skip_status = lambda platform, user: None
        self.plugin.skip_reason = lambda code: code
        panel = PanelScopes()
        panel.plugin, panel.bank_stats = self.plugin, self.cache
        panel.platforms = lambda: []
        panel.error_message = lambda code: code
        async def bot_id(platform):
            return self.incoming.bot_id
        panel.bot_id = bot_id
        original = self.queue.capture(self.incoming, "chat")
        for index in range(5):
            self.queue.references.record_skip(self.alice, str(index), "media_or_non_text", ("image",))
        self.queue.capture_assistant(original.task_id, "迟到的助手回复")
        rows = await panel.user_scopes()
        alice = next(row for row in rows if row["user_id"] == "alice")
        self.assertEqual(alice["summary"]["skip_warning"]["count"], 5)
        self.queue.capture(replace(self.incoming, message_id="next", timestamp=self.incoming.timestamp + 1), "chat")
        rows = await panel.user_scopes()
        self.assertNotIn("skip_warning", next(row for row in rows if row["user_id"] == "alice")["summary"])

    async def test_cold_cache_returns_immediately_and_parallel_polls_share_one_request(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(bank):
            entered.set()
            await release.wait()
            return {"total_documents": 0, "total_nodes": 0}
        self.client.stats.side_effect = delayed
        self.assertIsNone(self.cache.get(self.alice))
        await entered.wait()
        self.assertIsNone(self.cache.get(self.alice))
        self.assertEqual(self.client.stats.await_count, 1)
        release.set()
        await asyncio.gather(*self.cache.tasks.values())
        self.assertEqual(self.cache.get(self.alice), {"documents": 0, "memories": 0})
        self.assertEqual(self.client.stats.await_count, 1)

    async def test_generation_change_discards_late_totals_and_offline_is_distinct_from_zero(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(bank):
            entered.set()
            await release.wait()
            return {"total_documents": 12, "total_nodes": 20}
        self.client.stats.side_effect = delayed
        self.cache.get(self.alice)
        await entered.wait()
        self.queue.forget(self.alice["scope_id"])
        release.set()
        await asyncio.gather(*self.cache.tasks.values())
        self.assertFalse(self.cache.values)
        self.client.stats.side_effect = HindsightError("connection_error")
        self.assertIsNone(self.cache.get(self.bob))
        await asyncio.gather(*self.cache.tasks.values())
        self.assertIsNone(self.cache.get(self.bob))
