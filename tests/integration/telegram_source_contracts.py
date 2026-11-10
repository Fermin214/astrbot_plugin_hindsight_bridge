"""Real AstrBot/PTB objects with synthetic Telegram HTTP and message payloads."""

import argparse
import asyncio
import copy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
host_root = Path(os.environ["ASTRBOT_ROOT"])
if not host_root.is_absolute() or (host_root.exists() and any(host_root.iterdir())):
    raise RuntimeError("ASTRBOT_ROOT must be a dedicated empty test directory")
os.environ["ASTRBOT_DISABLE_METRICS"] = "1"

from astrbot.api.event import AstrMessageEvent
from astrbot.core.config.default import VERSION
from astrbot.core.message.components import Plain
from astrbot.core.platform.sources.telegram.tg_adapter import TelegramPlatformAdapter
from telegram import Update
from telegram.request import HTTPXRequest

for name, path in (("plugins", ROOT.parent), ("plugins.astrbot_plugin_hindsight_bridge", ROOT)):
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package

from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox, QueueError
from plugins.astrbot_plugin_hindsight_bridge.core.source import (
    native_header, native_login, prepare_bot_identity, source_identity,
)

USER = 9007199254740993


class Fixture:
    def __init__(self):
        self.bot_id = 100000001
        self.username = "SyntheticBot"
        self.host = TelegramPlatformAdapter({
            "id": "telegram-fixture", "type": "telegram", "enable": True,
            "telegram_token": "123456:synthetic-offline-token",
            "telegram_command_register": False, "telegram_command_auto_refresh": False,
        }, {}, asyncio.Queue())
        self.binding = None

    async def request(self, request, url, method, **kwargs):
        if not url.endswith("/getMe"):
            raise AssertionError("unexpected Telegram I/O endpoint")
        return 200, json.dumps({"ok": True, "result": {
            "id": self.bot_id, "is_bot": True, "first_name": "Synthetic",
            "username": self.username,
        }}).encode()

    async def start(self):
        await self.host._start_application()
        self.bind()

    def bind(self):
        login = native_login(self.host)
        assert login is not None
        self.binding = {
            "adapter": self.host, "client": self.host.get_client(),
            "bot_id": login.bot_id, "login": login, "invalid_login": False,
        }

    async def event(self, text="synthetic text", *, changes=None, mid=42):
        message = {
            "message_id": mid, "date": int(time.time()), "text": text,
            "chat": {"id": USER, "type": "private"},
            "from": {"id": USER, "is_bot": False, "first_name": "Synthetic user"},
            **(changes or {}),
        }
        raw = Update.de_json({"update_id": mid, "message": copy.deepcopy(message)}, self.host.client)
        abm = await self.host.convert_message(raw, types.SimpleNamespace(bot=self.host.client))
        assert abm is not None
        return self.host.create_event(abm)

    def header(self, event):
        return native_header(event, Plain, ["/"], adapter=self.host, binding=self.binding)

    async def close(self):
        if self.host.application.running:
            await self.host.application.stop()
        await self.host.application.shutdown()


async def main(output):
    assert VERSION == os.environ.get("ASTRBOT_TEST_VERSION", "4.29.4"), VERSION
    fixture, passed = Fixture(), []

    async def telegram_http(request, url, method, **kwargs):
        return await fixture.request(request, url, method, **kwargs)

    with patch.object(HTTPXRequest, "do_request", telegram_http):
        try:
            assert native_login(fixture.host) is None
            await fixture.start()
            event = await fixture.event()
            incoming = fixture.header(event)
            assert incoming is not None and incoming.user_id == str(USER)
            assert incoming.bot_id == "100000001" and incoming.bot_id != event.get_self_id()
            assert incoming.timestamp == event.message_obj.raw_message.message.date.timestamp()
            assert source_identity(event, incoming, fixture.host, binding=fixture.binding).producer is fixture.host.client
            passed.append("native_login_uses_initialized_numeric_bot_id_and_exact_large_user_id")
            for label, changes in (
                ("bot_sender", {"from": {"id": USER, "is_bot": True, "first_name": "Bot"}}),
                ("group", {"chat": {"id": USER, "type": "group"}}),
                ("different_chat", {"chat": {"id": USER + 1, "type": "private"}}),
                ("zero_message_id", {"message_id": 0}),
                ("future_date", {"date": int(time.time()) + 301}),
                ("missing_date", {"date": 0}),
                ("caption", {"caption": "synthetic media caption"}),
                ("media", {"photo": [{"file_id": "synthetic", "file_unique_id": "synthetic", "width": 1, "height": 1}]}),
                ("reply", {"reply_to_message": {
                    "message_id": 1, "date": int(time.time()), "text": "quoted",
                    "chat": {"id": USER, "type": "private"},
                    "from": {"id": USER, "is_bot": False, "first_name": "Quoted"},
                }}),
                ("forward", {"forward_origin": {
                    "type": "hidden_user", "date": int(time.time()), "sender_user_name": "Synthetic",
                }}),
                ("edited", {"edit_date": int(time.time())}),
                ("business", {"business_connection_id": "synthetic-connection"}),
                ("via_bot", {"via_bot": {"id": 777, "is_bot": True, "first_name": "Inline"}}),
                ("unknown", {"future_protocol_field": {"text": "synthetic"}}),
            ):
                assert fixture.header(await fixture.event(changes=changes)) is None, label
            passed.append("nonordinary_private_media_reply_forward_edit_business_and_unknown_fields_rejected")

            for entity in ("bold", "italic", "code", "bot_command"):
                formatted = await fixture.event(changes={"entities": [{"type": entity, "offset": 0, "length": 4}]})
                assert fixture.header(formatted) is not None, entity
            for entity in (
                {"type": "text_link", "offset": 0, "length": 4, "url": "https://example.invalid"},
                {"type": "mention", "offset": 0, "length": 4},
            ):
                assert fixture.header(await fixture.event(changes={"entities": [entity]})) is None
            command = await fixture.event("/hindsight@SyntheticBot pause")
            assert fixture.header(command).text == "/hindsight pause"
            command.message_str = "hindsight pause"
            command.is_at_or_wake_command = True
            assert fixture.header(command).text == "/hindsight pause"
            command.message_str = "unrelated"
            assert fixture.header(command) is None
            passed.append("plain_formatting_and_native_command_suffix_supported_transformed_entities_rejected")

            old = await fixture.event()
            fixture.bot_id = 100000002
            await fixture.host._recreate_application()
            assert native_login(fixture.host) is None
            await fixture.start()
            assert fixture.header(old) is None
            fresh = fixture.header(await fixture.event())
            assert fresh.bot_id == "100000002"
            assert old.client is not fixture.host.client
            passed.append("old_event_first_observed_after_bot_replacement_cannot_use_new_login")
            fixture.username = "RenamedSyntheticBot"
            await fixture.host.client.get_me()
            renamed = fixture.header(await fixture.event())
            assert renamed.scope("deployment").id == fresh.scope("deployment").id
            passed.append("username_changes_keep_numeric_bot_scope")
            context = types.SimpleNamespace(
                get_config=lambda: {"platform": [fixture.host.config]},
                get_platform_inst=lambda _: fixture.host,
            )
            assert await prepare_bot_identity(context, "telegram-fixture") == "100000002"

            with tempfile.TemporaryDirectory() as directory:
                queue = Outbox(
                    Path(directory), "telegram-deployment", "http://memory.test",
                    credential_fingerprint="synthetic",
                )
                try:
                    pair = ("telegram-fixture", str(USER))
                    baseline = time.time()
                    queue.sync_permissions([pair])
                    queue.activate(fresh, queue.access.authorization(*pair)["revision"])
                    assert not queue.accepts(fresh)
                    with (
                        patch.object(time, "time", return_value=baseline + 2),
                        patch.object(sys.modules[AstrMessageEvent.__module__], "time", return_value=baseline + 2),
                    ):
                        captured_event = fixture.header(await fixture.event(mid=100))
                        captured = queue.capture(captured_event, "conversation")
                        assert not queue.capture(captured_event, "another-conversation").created
                        frozen = dict(queue.get(captured.task_id))
                        assert json.loads(frozen["payload"])["items"][0]["metadata"]["time_source"] == "telegram.message.date"
                        command = fixture.header(await fixture.event("/hindsight pause", mid=101))
                        source, scope = command.scope(queue.deployment_id), queue.scope(command)
                        change = queue.access.command(command, source, scope, "mode", ("paused",))
                        queue.access.run(change, scope, lambda: queue.set_mode(
                            scope["scope_id"], "paused", journal_only=True,
                        ))
                        queue.apply_control()
                        try:
                            queue.access.command(command, source, queue.scope_owner(source.id), "mode", ("paused",))
                        except QueueError as error:
                            assert str(error) == "command_already_applied"
                        else:
                            raise AssertionError("replayed command accepted")
                        queue.sync_permissions([])
                        assert not queue.current(frozen)
                        queue.sync_permissions([pair])
                        queue.activate(captured_event, queue.access.authorization(*pair)["revision"])
                        assert not queue.accepts(captured_event)
                        old_bank = queue.scope_owner(source.id)["bank"]
                        queue.forget(source.id)
                        assert queue.scope_owner(source.id)["bank"] != old_bank
                    passed.append("native_time_provenance_deduplication_command_receipt_regrant_and_forget")
                finally:
                    queue.close()
            report = {
                "astrbot": VERSION, "python_telegram_bot": importlib.metadata.version("python-telegram-bot"),
                "evidence_layer": "native_adapter_synthetic_http_and_updates",
                "real_platform_login": False, "hindsight_calls": False, "passed": passed,
                "source_sha256": {
                    name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                    for name in ("main.py", "core/source.py", "core/telegram_source.py", "core/panel.py")
                },
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))
        finally:
            await fixture.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args().output))
