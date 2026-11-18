"""Native Lark conversion with synthetic payloads and real Bridge control data."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[2]


class Fixture:
    def __init__(self, *, instance="lark-fixture", appid="cli_fixture", bot="ou_fixturebot", mode="socket"):
        from astrbot.core.platform.sources.lark.lark_adapter import LarkPlatformAdapter

        self.queue = asyncio.Queue()
        self.host = LarkPlatformAdapter({
            "id": instance, "type": "lark", "enable": True,
            "app_id": appid, "app_secret": "synthetic-offline-secret",
            "lark_connection_mode": mode,
        }, {}, self.queue)
        # AstrBot normally populates this from the authenticated bot-info API.
        self.host.bot_open_id = bot
        self.host._user_name_cache["ou_fixtureuser"] = ("Synthetic user", time.time() + 3600)

    async def event(self, *, text="  synthetic text  ", changes=None, mid="om_fixture", offset=0):
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        payload = P2ImMessageReceiveV1({
            "schema": "2.0",
            "header": {"event_id": mid, "event_type": "im.message.receive_v1", "app_id": self.host.appid},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_fixtureuser"}, "sender_type": "user"},
                "message": {
                    "message_id": mid, "chat_id": "oc_fixturechat", "chat_type": "p2p",
                    "message_type": "text", "create_time": str(int((time.time() + offset) * 1000)),
                    "content": json.dumps({"text": text}), **(changes or {}),
                },
            },
        })
        with patch.object(self.host, "_build_reply_from_parent_id", AsyncMock(return_value=None)):
            await self.host.convert_msg(payload)
        return self.queue.get_nowait()

    def binding(self):
        from plugins.astrbot_plugin_hindsight_bridge.core.source import native_login

        login = native_login(self.host)
        assert login is not None
        return {
            "adapter": self.host, "client": self.host.get_client(),
            "bot_id": login.bot_id, "login": login, "invalid_login": False,
        }


async def main(output):
    host_root = Path(os.environ["ASTRBOT_ROOT"])
    if not host_root.is_absolute() or host_root.exists() and any(host_root.iterdir()):
        raise RuntimeError("ASTRBOT_ROOT must be a dedicated empty test directory")
    from astrbot.core.config.default import VERSION
    from astrbot.api.event import AstrMessageEvent
    from astrbot.core.message.components import Plain
    from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox
    from plugins.astrbot_plugin_hindsight_bridge.core.source import (
        native_header, native_login, prepare_bot_identity, source_identity,
    )

    assert VERSION == os.environ.get("ASTRBOT_TEST_VERSION", "4.29.4"), VERSION
    passed = []
    for mode in ("socket", "webhook"):
        fixture = Fixture(mode=mode)
        binding = fixture.binding()
        header = lambda event: native_header(event, Plain, ["/"], adapter=fixture.host, binding=binding)
        event = await fixture.event()
        incoming = header(event)
        assert incoming and incoming.text == "  synthetic text  "
        assert incoming.timestamp == int(event.message_obj.raw_message.create_time) / 1000
        assert incoming.user_id == "ou_fixtureuser" and incoming.message_id == "om_fixture"
        assert source_identity(event, incoming, fixture.host, binding=binding).producer is fixture.host.lark_api
        passed.append(mode + "_native_sdk_text_conversion_original_time_and_api_producer")
        spec = importlib.util.spec_from_file_location("lark_probe", ROOT / "tests/integration/platform_probe/probe.py")
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        sample_report = probe.project(event, fixture.host, b"synthetic-fingerprint-key")
        assert sample_report["raw_id_matches_host"] and sample_report["text_components_match"]
        serialized = json.dumps(sample_report)
        assert all(value not in serialized for value in (
            "ou_fixtureuser", "ou_fixturebot", "cli_fixture", "synthetic text", "synthetic-offline-secret",
        ))
        passed.append(mode + "_field_probe_excludes_platform_credentials_ids_and_text")
        command = await fixture.event(text="/hindsight pause")
        command.message_str = "hindsight pause"
        command.is_at_or_wake_command = True
        assert header(command).text == "/hindsight pause"
        command.message_str = "unrelated"
        assert header(command) is None
        passed.append(mode + "_command_prefix_requires_exact_original_text")

        for field, value in (
            ("chat_type", "group"), ("chat_type", "unknown"),
            ("message_type", "post"), ("message_type", "image"),
            ("message_type", "audio"), ("message_type", "file"),
            ("parent_id", "om_parent"), ("root_id", "om_root"), ("thread_id", "omt_thread"),
            ("mentions", [object()]), ("chat_id", None), ("message_id", "om_other"),
            ("create_time", None), ("create_time", int(time.time())),
            ("create_time", int((time.time() + 301) * 1000)), ("create_time", True),
            ("update_time", int((time.time() + 2) * 1000)), ("update_time", False),
            ("content", "{}"), ("content", '{"text":null}'), ("content", "[]"),
            ("content", '{"text":"synthetic text","image_key":"extra"}'),
            ("content", '{"text":"   "}'), ("content", "{invalid"),
            ("content", json.dumps({"text": "x" * 8001})),
        ):
            sample = await fixture.event()
            setattr(sample.message_obj.raw_message, field, value)
            assert header(sample) is None, (mode, field, type(value))
        passed.append(mode + "_media_references_edits_unknown_chat_invalid_ids_and_times_rejected")
        sample = await fixture.event()
        sample.message_obj.raw_message.update_time = sample.message_obj.raw_message.create_time
        assert header(sample)
        sample.message_obj.message[0].text = "changed"
        assert header(sample) is None
        sample = await fixture.event()
        sample.created_at = float("nan")
        assert header(sample) is None
        sample = await fixture.event()
        sample.message_obj.sender.user_id = fixture.host.bot_open_id
        assert header(sample) is None
        passed.append(mode + "_host_component_sender_and_receive_clock_checks")

        other = Fixture(instance=fixture.host.meta().id, appid="cli_another", bot="ou_anotherbot")
        old = await fixture.event()
        # Even when self ID is made to look new, the old API reference is rejected.
        old.message_obj.self_id = other.host.bot_open_id
        assert native_header(old, Plain, adapter=other.host, binding=other.binding()) is None
        same = Fixture(instance=fixture.host.meta().id)
        assert same.binding()["bot_id"] == binding["bot_id"]
        assert native_header(await fixture.event(), Plain, adapter=same.host, binding=same.binding()) is None
        assert native_header(await same.event(), Plain, adapter=same.host, binding=same.binding())
        passed.append(mode + "_same_account_reload_keeps_scope_but_rejects_old_event_client")
        passed.append(mode + "_different_application_rejects_queued_events")

        context = types.SimpleNamespace(
            get_config=lambda: {"platform": [fixture.host.config]},
            get_platform_inst=lambda _: fixture.host,
        )
        assert await prepare_bot_identity(context, fixture.host.meta().id) == binding["bot_id"]
        fixture.host.bot_open_id = ""
        assert native_login(fixture.host) is None
        assert await prepare_bot_identity(context, fixture.host.meta().id) == ""
        fixture.host.bot_open_id = "ou_fixturebot"
        passed.append(mode + "_unresolved_bot_info_cannot_authorize_display_name")

        with tempfile.TemporaryDirectory() as directory:
            queue = Outbox(Path(directory), "lark-deployment", "http://memory.test",
                           credential_fingerprint="synthetic")
            try:
                pair = (fixture.host.meta().id, incoming.user_id)
                queue.sync_permissions([pair])
                queue.activate(incoming, queue.access.authorization(*pair)["revision"])
                assert not queue.accepts(incoming)
                future = time.time() + 2
                with (
                    patch.object(time, "time", return_value=future),
                    patch.object(sys.modules[AstrMessageEvent.__module__], "time", return_value=future),
                ):
                    fresh = header(await fixture.event(mid="om_fresh"))
                    assert queue.accepts(fresh)
                    first = queue.capture(fresh, "synthetic-conversation")
                    assert first.created and not queue.capture(fresh, "another-conversation").created
                    payload = json.loads(queue.get(first.task_id)["payload"])
                    assert payload["items"][0]["metadata"]["time_source"] == "lark.create_time"
                    other_scope = incoming.scope("lark-deployment")
                    assert other_scope.bank() != incoming.scope("other-deployment").bank()
                    queue.access.observe_bot(pair[0], "lark", other.binding()["bot_id"])
                    assert not queue.access.authorization(*pair)["enabled"]
                    queue.access.observe_bot(pair[0], "lark", binding["bot_id"])
                    assert not queue.access.authorization(*pair)["enabled"]
                passed.append(mode + "_control_store_authorization_time_deduplication_and_application_revocation")
            finally:
                queue.close()
    report = {
        "astrbot": VERSION, "platform": "lark", "lark_oapi": importlib.metadata.version("lark-oapi"),
        "platform_peer": "synthetic SDK payloads and bot-info identity",
        "passed": passed,
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ("core/lark_source.py", "core/source.py", "main.py", "core/panel.py")
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    for name, path in (("plugins", ROOT.parent), ("plugins.astrbot_plugin_hindsight_bridge", ROOT)):
        package = types.ModuleType(name)
        package.__path__ = [str(path)]
        sys.modules[name] = package
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args().output))
