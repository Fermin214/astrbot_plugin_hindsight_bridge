"""Native Weixin adapter, synthetic protocol payloads, real Bridge control data."""

import argparse
import asyncio
import copy
import hashlib
import importlib
import importlib.util
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
from astrbot.core.platform.sources.weixin_oc.weixin_oc_adapter import WeixinOCAdapter

for name, path in (("plugins", ROOT.parent), ("plugins.astrbot_plugin_hindsight_bridge", ROOT)):
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package

from plugins.astrbot_plugin_hindsight_bridge.core.outbox import Outbox, QueueError
from plugins.astrbot_plugin_hindsight_bridge.core.source import (
    native_header, native_login, prepare_bot_identity, source_identity,
)

spec = importlib.util.spec_from_file_location("field_probe", Path(__file__).parent / "platform_probe/probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class Fixture:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.host = WeixinOCAdapter({
            "id": "weixin-fixture", "type": "weixin_oc", "enable": True,
            "weixin_oc_account_id": "synthetic-bot@im.bot",
            "weixin_oc_token": "synthetic-offline-token",
        }, {}, self.queue)
        self.bind()

    def bind(self):
        login = native_login(self.host)
        assert login is not None
        self.binding = {
            "adapter": self.host, "client": self.host.get_client(),
            "bot_id": login.account_id, "login": login, "invalid_login": False,
        }

    async def event(self, *, changes=None, text="  synthetic text  ", offset=0):
        payload = {
            "message_id": "9007199254740993123",
            "from_user_id": "synthetic-user@im.wechat",
            "to_user_id": "synthetic-bot@im.bot",
            "message_type": 1, "message_state": 2,
            "create_time_ms": int((time.time() + offset) * 1000),
            "item_list": [{"type": 1, "text_item": {"text": text}}],
            "context_token": "synthetic-private-context",
            **(changes or {}),
        }
        # Media download failures must not turn mixed input into plain text.
        with patch.object(self.host, "_resolve_inbound_media_component", return_value=None):
            await self.host._handle_inbound_message(copy.deepcopy(payload))
        return self.queue.get_nowait()

    def header(self, event):
        return native_header(event, Plain, ["/"], adapter=self.host, binding=self.binding)


async def main(output):
    assert VERSION == os.environ.get("ASTRBOT_TEST_VERSION", "4.29.4"), VERSION
    fixture, passed = Fixture(), []
    try:
        event = await fixture.event()
        incoming = fixture.header(event)
        assert incoming is not None and incoming.text == "synthetic text"
        assert incoming.message_id == "9007199254740993123"
        assert incoming.bot_id == "synthetic-bot@im.bot"
        assert incoming.timestamp == event.message_obj.raw_message["create_time_ms"] / 1000
        assert source_identity(event, incoming, fixture.host, binding=fixture.binding).producer is fixture.host.client
        passed.append("raw_recipient_large_message_id_millisecond_timestamp_and_native_producer")
        record = probe.project(event, fixture.host, b"synthetic-fingerprint-key")
        serialized = json.dumps(record)
        for secret in (
            "synthetic-private-context", "synthetic-offline-token", "synthetic text",
            "synthetic-user@im.wechat", "synthetic-bot@im.bot", "9007199254740993123",
        ):
            assert secret not in serialized, secret
        assert record["raw_id_matches_host"] and record["recipient_matches_observed_account"]
        passed.append("field_probe_excludes_credentials_raw_ids_and_text")
        probe_package = types.ModuleType("field_sampler")
        probe_package.__path__ = [str(Path(__file__).parent / "platform_probe")]
        sys.modules["field_sampler"] = probe_package
        sampler_type = importlib.import_module("field_sampler.main").PlatformProbe
        sampler_context = types.SimpleNamespace(get_platform_inst=lambda _: fixture.host)
        sampler_config = {
            "enabled": True, "platform_instance_id": "weixin-fixture",
            "user_id": "synthetic-user@im.wechat",
        }
        sampler = sampler_type(sampler_context, sampler_config)
        arm = await fixture.event(text="/bridge_probe arm text")
        arm.message_str = "bridge_probe arm text"
        assert len([reply async for reply in sampler.sample(arm)]) == 1
        sampled = await fixture.event(text="synthetic sample content")
        assert len([reply async for reply in sampler.sample(sampled)]) == 1
        reports = list(sampler.directory.glob("*.json"))
        assert len(reports) == 1
        assert json.loads(reports[0].read_text())["sample"] == "text"
        assert "synthetic sample content" not in reports[0].read_text()
        reloaded = sampler_type(sampler_context, sampler_config)
        assert reloaded.key == sampler.key and not reloaded.samples.armed
        passed.append("native_sampler_handles_host_wake_prefix_and_reload_preserves_only_fingerprint_key")

        for label, changes in (
            ("missing_id", {"message_id": None}),
            ("fallback_id", {"message_id": None, "msg_id": "42"}),
            ("missing_time", {"create_time_ms": None}),
            ("seconds_in_millisecond_field", {"create_time_ms": int(time.time())}),
            ("fractional_milliseconds", {"create_time_ms": time.time() * 1000}),
            ("future_time", {"create_time_ms": int((time.time() + 301) * 1000)}),
            ("bot_direction", {"message_type": 2}),
            ("generating", {"message_state": 1}),
            ("missing_direction", {"message_type": None}),
            ("unknown_state", {"message_state": 90}),
            ("group", {"group_id": "synthetic-group"}),
            ("delete", {"delete_time_ms": int(time.time() * 1000)}),
            ("new_later_update", {"message_state": 0, "update_time_ms": int((time.time() + 2) * 1000)}),
            ("future_update", {"update_time_ms": int((time.time() + 301) * 1000)}),
            ("boolean_update", {"update_time_ms": False}),
            ("backwards_update", {"update_time_ms": int((time.time() - 10) * 1000)}),
            ("different_recipient", {"to_user_id": "another-bot"}),
            ("unknown_extension", {"unknown_extension": {"text": "synthetic"}}),
            ("empty_container_extension", {"unknown_extension": {}}),
            ("populated_scalar_extension", {"unknown_extension": True}),
            ("unknown_empty_scalar", {"unknown_extension": 0}),
            ("root_relation", {"root_id": 1}),
            ("parent_relation", {"parent_id": 1}),
            ("boolean_relation", {"root_id": False}),
            ("unusable_user_id", {"from_user_id": "user:FriendMessage:another"}),
            ("media", {"item_list": [
                {"type": 1, "text_item": {"text": "synthetic"}},
                {"type": 2, "image_item": {"media": {}}},
            ]}),
            ("voice_transcription", {"item_list": [
                {"type": 3, "voice_item": {"text": "synthetic"}},
            ]}),
            ("reference", {"item_list": [
                {"type": 1, "text_item": {"text": "synthetic"}, "ref_msg": {}},
            ]}),
            ("incomplete_item", {"item_list": [
                {"type": 1, "text_item": {"text": "synthetic"}, "is_completed": False},
            ]}),
            ("buttons", {"item_list": [
                {"type": 1, "text_item": {"text": "synthetic"}, "button_item_list": [{}]},
            ]}),
            ("mentions", {"item_list": [
                {"type": 1, "text_item": {"text": "synthetic"}, "at_bot_username_list": ["bot"]},
            ]}),
            ("malformed_control_list", {"item_list": [
                {"type": 1, "text_item": {"text": "synthetic"}, "button_item_list": ""},
            ]}),
        ):
            assert fixture.header(await fixture.event(changes=changes)) is None, label
        passed.append("native_fallback_media_reference_updates_direction_and_unknown_fields_rejected")

        created = int(time.time() * 1000) - 100
        completed = await fixture.event(changes={
            "create_time_ms": created, "update_time_ms": created + 50,
            "root_id": 0, "parent_id": 0,
            "item_list": [{
                "type": 1, "text_item": {"text": "synthetic text"},
                "create_time_ms": created, "update_time_ms": created + 25,
                "is_completed": True, "ref_msg": None, "image_item": None,
                "button_item_list": [], "at_bot_username_list": [],
            }],
        })
        assert fixture.header(completed).timestamp == created / 1000
        record = probe.project(completed, fixture.host, b"synthetic-fingerprint-key")
        assert record["unknown_field_count"] == 2
        assert all(row["empty_scalar"] for row in record["unknown_fields"])
        assert all(row["empty_scalar"] or row["empty_container"] for row in record["items"][0]["unknown_fields"])
        passed.append("finished_completion_timestamps_zero_relations_and_empty_controls_preserve_original_source")

        multiline = await fixture.event(changes={"item_list": [
            {"type": 1, "text_item": {"text": " first "}},
            {"type": 1, "text_item": {"text": "  "}},
            {"type": 1, "text_item": {"text": " second "}},
        ]})
        assert fixture.header(multiline).text == "first\nsecond"
        command = await fixture.event(text="/hindsight pause")
        command.message_str = "hindsight pause"
        command.is_at_or_wake_command = True
        assert fixture.header(command).text == "/hindsight pause"
        command.message_str = "unrelated"
        assert fixture.header(command) is None
        passed.append("native_multitext_and_exact_wake_prefix_conversion")

        old = await fixture.event()
        fixture.host.account_id = "another-bot"
        fixture.host.config["weixin_oc_account_id"] = "another-bot"
        fixture.bind()
        assert fixture.header(old) is None
        fixture.host.account_id = "synthetic-bot@im.bot"
        fixture.host.config["weixin_oc_account_id"] = fixture.host.account_id
        fixture.bind()
        fixture.host.token = fixture.host.client.token = "rotated-synthetic-token"
        assert fixture.header(await fixture.event()) is not None
        passed.append("first_observation_after_account_change_rejects_old_queued_recipient")
        passed.append("same_account_token_rotation_keeps_native_scope")

        context = types.SimpleNamespace(
            get_config=lambda: {"platform": [fixture.host.config]},
            get_platform_inst=lambda _: fixture.host,
        )
        assert await prepare_bot_identity(context, "weixin-fixture") == fixture.host.account_id
        fixture.host.token = None
        assert await prepare_bot_identity(context, "weixin-fixture") == ""
        fixture.host.token = "restored-synthetic-token"
        passed.append("management_identity_uses_account_id_and_requires_native_login")

        with tempfile.TemporaryDirectory() as directory:
            queue = Outbox(
                Path(directory), "weixin-deployment", "http://memory.test",
                credential_fingerprint="synthetic",
            )
            try:
                baseline = time.time()
                pair = ("weixin-fixture", "synthetic-user@im.wechat")
                queue.sync_permissions([pair])
                queue.activate(incoming, queue.access.authorization(*pair)["revision"])
                assert not queue.accepts(incoming)
                with (
                    patch.object(time, "time", return_value=baseline + 2),
                    patch.object(sys.modules[AstrMessageEvent.__module__], "time", return_value=baseline + 2),
                ):
                    fresh = fixture.header(await fixture.event(changes={"message_id": "100"}))
                    assert queue.accepts(fresh)
                    captured = queue.capture(fresh, "synthetic-conversation")
                    frozen = dict(queue.get(captured.task_id))
                    assert not queue.capture(fresh, "another-conversation").created
                    updated = fixture.header(await fixture.event(changes={
                        "message_id": "100", "create_time_ms": int(fresh.timestamp * 1000),
                        "update_time_ms": int((baseline + 2) * 1000) + 1,
                    }))
                    assert updated is not None and not queue.capture(updated, "another-conversation").created
                    changed = fixture.header(await fixture.event(text="changed text", changes={
                        "message_id": "100", "create_time_ms": int(fresh.timestamp * 1000),
                        "update_time_ms": int((baseline + 2) * 1000) + 2,
                    }))
                    try:
                        queue.capture(changed, "another-conversation")
                    except QueueError as error:
                        assert str(error) == "message_identity_content_conflict"
                    else:
                        raise AssertionError("updated message created another memory task")
                    raw_material = json.loads(frozen["payload"])
                    assert raw_material["items"][0]["metadata"]["time_source"] == "weixin_oc.create_time_ms"
                    owner = queue.scope(fresh)
                    old_bank = owner["bank"]
                    paused = fixture.header(await fixture.event(
                        text="/hindsight pause", changes={"message_id": "101"},
                    ))
                    source = paused.scope(queue.deployment_id)
                    change = queue.access.command(paused, source, owner, "mode", ("paused",))
                    queue.access.run(change, owner, lambda: queue.set_mode(
                        owner["scope_id"], "paused", journal_only=True,
                    ))
                    queue.apply_control()
                    try:
                        queue.access.command(paused, source, queue.scope_owner(source.id), "mode", ("paused",))
                    except QueueError as error:
                        assert str(error) == "command_already_applied"
                    else:
                        raise AssertionError("replayed native command accepted")
                    queue.sync_permissions([])
                    assert not queue.accepts(fresh) and not queue.current(frozen)
                    queue.sync_permissions([pair])
                    queue.activate(fresh, queue.access.authorization(*pair)["revision"])
                    assert not queue.accepts(fresh)
                    queue.forget(source.id)
                    assert queue.scope_owner(source.id)["bank"] != old_bank
                passed.append("native_capture_deduplication_time_provenance_revoke_regrant_and_forget")
                passed.append("native_state_changing_command_is_consumed_once")
                passed.append("finished_message_updates_deduplicate_and_changed_text_preserves_frozen_task")
            finally:
                queue.close()
        report = {
            "astrbot": VERSION, "evidence_layer": "native_adapter_synthetic_protocol_payloads",
            "official_protocol_reference": "@tencent-weixin/openclaw-weixin 2.4.10",
            "real_platform_login": False, "hindsight_calls": False, "passed": passed,
            "source_sha256": {
                name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                for name in ("main.py", "core/source.py", "core/weixin_source.py", "core/panel.py")
            },
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        await fixture.host.client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args().output))
